"""Sprites execution environment (standalone Hermes plugin).

Originally authored by Kyle McLaren (@kylemclaren, Fly.io) as hermes-agent
PR #30112, hardened in salvage PR #93523. Extracted to this standalone
plugin repo when terminal backends became pluggable (hermes-agent PR #94400).

Uses the sprites-py SDK (https://github.com/superfly/sprites-py) to run
commands in Sprites — stateful cloud sandboxes on Fly.io, with
checkpoint & restore. Persistent by default: each Sprite outlives the session
and is reused via a deterministic, profile-scoped name — ``hermes-{task_id}``
on the default profile, ``hermes-{display}-{digest}`` on named profiles (see
``_resolve_sprite_name`` for the full contract). Cleanup leaves the Sprite
running when ``persistent_filesystem`` is True; the Sprite is deleted otherwise.
"""

import hashlib
import logging
import re
import shlex
import threading
import uuid
from pathlib import Path

from tools.environments.base import BaseEnvironment

try:  # hermes-agent >= v0.21.4 (Sep 2026 decomposition moved it)
    from tools.environments.base_output import _ThreadedProcessHandle
except ImportError:  # older hermes-agent kept it in base
    from tools.environments.base import _ThreadedProcessHandle
from tools.environments.file_sync import (
    FileSyncManager,
    iter_sync_files,
)

logger = logging.getLogger(__name__)


#: Hard bound on generated Sprite names. The name feeds the
#: ``{name}-random.sprites.app`` hostname, so it must fit a DNS label.
_MAX_NAME_LEN = 63

#: Hex chars of identity digest carried in non-legacy names (48 bits — a
#: collision needs ~2^24 coexisting identities, versus 2^12 at the previous
#: 6 chars, where a collision between valid profile names was demonstrated).
_DIGEST_LEN = 12


def _collapse_slug(value: str) -> str:
    """Plain DNS-safe collapse: lowercase, non-alnum runs become one hyphen."""
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def _identity_digest(*parts: str) -> str:
    """Collision-resistant digest of an ordered tuple of raw strings.

    Each component is length-prefixed before hashing, so the encoding is
    unambiguous: ``("a\\x1fb", "c")`` and ``("a", "b\\x1fc")`` digest
    differently no matter what bytes the components contain.
    """
    h = hashlib.sha256()
    for part in parts:
        b = (part or "").encode("utf-8")
        h.update(len(b).to_bytes(4, "big"))
        h.update(b)
    return h.hexdigest()[:_DIGEST_LEN]


def _bounded_name(display: str, digest: str) -> str:
    """Compose ``hermes-{display}-{digest}`` within ``_MAX_NAME_LEN``.

    The digest is the authoritative identity and is never truncated; the
    human-readable display slug absorbs all the shortening.
    """
    budget = _MAX_NAME_LEN - len("hermes-") - 1 - len(digest)
    display = (display or "")[:budget].strip("-")
    if display:
        return f"hermes-{display}-{digest}"
    return f"hermes-{digest}"


def _slugify_name_component(value: str) -> str:
    """Reduce an arbitrary string to a Sprite/Fly-safe name component.

    Sprite names are DNS-ish: lowercase ``[a-z0-9-]`` with no leading/trailing
    or doubled hyphens. Anything else (``/``, ``.``, uppercase, unicode from a
    profile directory or subagent id) is collapsed to a single hyphen.

    Because a Sprite name is a durable trust boundary (it selects a live VM,
    not just a label), lossy collapsing must not merge distinct inputs: when
    the slug is not byte-identical to the input, a short hash of the raw value
    is appended so e.g. ``team_prod`` and ``team-prod`` stay distinct. Values
    that are already slug-clean — every name the backend historically
    produced — are unchanged, so existing Sprites keep resolving. (A
    crafted clean value that equals another value's slug+hash output can
    still impersonate it; within one component this is accepted — task ids
    are internal, not attacker-supplied across trust domains.)
    """
    raw = value or ""
    slug = _collapse_slug(raw)
    if slug == raw:
        return slug
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]
    return f"{slug}-{digest}" if slug else digest


def _resolve_profile_identity() -> str | None:
    """Raw profile identity for Sprite naming, derived from HERMES_HOME.

    Returns ``None`` for the default profile (``~/.hermes`` or
    ``~/.hermes/profiles/default``), the profile directory name for a
    standard named profile, and a unique ``home:<path>`` identity for a
    custom HERMES_HOME outside the profiles tree — a nonstandard home is
    its own trust domain and must never silently share the default
    profile's Sprite.

    Deliberately does NOT use ``file_safety._resolve_active_profile_name``:
    that helper swallows resolution failures and returns ``"default"``,
    which for a durable sandbox identity is a fail-open into another trust
    domain. Here a resolution failure raises instead (fail closed).
    """
    try:
        from agent.file_safety import _hermes_home_path, _hermes_root_path
        home = _hermes_home_path().resolve()
        root = _hermes_root_path().resolve()
    except Exception as e:
        raise RuntimeError(
            "Sprites backend could not resolve the active Hermes profile; "
            f"refusing to fall back to the default profile's Sprite: {e}"
        ) from e
    if home == root:
        return None
    try:
        rel = home.relative_to(root / "profiles")
    except ValueError:
        return f"home:{home}"
    name = rel.parts[0] if rel.parts else None
    if name is None or name == "default":
        return None
    return name


def _resolve_sprite_name(task_id: str) -> str:
    """Deterministic, profile-scoped Sprite name.

    A Sprite is persistent and resumed *by name*, so its name is the durable
    identity of a session's live sandbox (processes, sockets, PID space — not
    just a filesystem snapshot). Naming contract:

    - Default profile, short task: ``hermes-{task_slug}`` — byte-compatible
      with the historical names, so already-created Sprites keep resolving.
      Clean task ids are verbatim-injective; lossy ones carry a
      per-component hash (see ``_slugify_name_component``).
    - Named profile / custom HERMES_HOME: ``hermes-{display}-{digest12}``
      where the digest covers the raw ``(profile, task_id)`` pair via a
      length-prefixed encoding (see ``_identity_digest``). The digest is the
      authoritative identity: it is collision-resistant across component
      boundaries, display-slug collisions, and separator forgery; the
      display prefix is cosmetic and is truncated to keep the whole name
      within a DNS label.
    - Any name that would exceed the DNS bound is composed via
      ``_bounded_name`` with the digest preserved intact.

    Profile resolution failure raises rather than falling back: silently
    entering the default profile's durable namespace would put a named
    profile's session inside another trust domain's live VM.
    """
    profile_raw = _resolve_profile_identity()
    if profile_raw is None:
        task_slug = _slugify_name_component(task_id) or "default"
        legacy = f"hermes-{task_slug}"
        if len(legacy) <= _MAX_NAME_LEN:
            return legacy
        # Too long for a DNS label (e.g. a session-keyed task id): fall to
        # the bounded form; the pair digest carries the identity.
        return _bounded_name(
            _collapse_slug(task_id), _identity_digest("", task_id or "")
        )
    profile_slug = _collapse_slug(profile_raw) or "profile"
    task_display = _collapse_slug(task_id) or "default"
    digest = _identity_digest(profile_raw, task_id or "")
    return _bounded_name(f"{profile_slug}-{task_display}", digest)


def _ephemeral_sprite_name(task_id: str) -> str:
    """Unique, non-resumable Sprite name for a non-persistent run.

    Ephemeral Sprites are created fresh and deleted on cleanup; their names
    deliberately contain a random component so no two runs — concurrent or
    sequential, same process or not — can ever attach the same live VM, and
    a stale survivor of a crashed run is never silently resumed.
    """
    nonce = uuid.uuid4().hex[:12]
    display = _collapse_slug(task_id) or "default"
    return _bounded_name(f"eph-{display}", nonce)


def _resolve_labels(
    labels: list[str] | None,
    auto_tags: bool,
    task_id: str,
) -> list[str]:
    """Compute the final label set for a sprite.

    Combines user-configured static labels with auto-derived tags (if
    ``auto_tags`` is True). Labels are sanitized (stripped, non-empty,
    de-duplicated preserving order).
    """
    result = list(labels or [])
    if auto_tags:
        result.append("hermes")
        task_slug = _collapse_slug(task_id)
        if task_slug and task_slug != "default":
            result.append(f"task-{task_slug}")
        profile = _resolve_profile_identity()
        if profile:
            result.append(f"profile-{_collapse_slug(profile)}")
    seen: set[str] = set()
    clean: list[str] = []
    for label in result:
        label = (label or "").strip()
        if label and label not in seen:
            seen.add(label)
            clean.append(label)
    return clean



class SpritesEnvironment(BaseEnvironment):
    """Sprites backend: stateful cloud sandboxes on Fly.io.

    Spawn-per-call via ``_ThreadedProcessHandle`` wrapping blocking
    ``sprite.command(...).combined_output()`` calls. The SDK timeout is
    used (rather than wrapping the shell), since the SDK already cancels
    the underlying WebSocket exec on deadline.
    """

    _stdin_mode = "heredoc"

    def __init__(
        self,
        cwd: str = "/root",
        timeout: int = 60,
        persistent_filesystem: bool = True,
        task_id: str = "default",
        labels: list[str] | None = None,
        auto_tags: bool = False,
        provision_script: str | None = None,
        provision_inline: str | None = None,
        provision_best_effort: bool = False,
        provision_timeout: int = 600,
    ):
        requested_cwd = cwd
        super().__init__(cwd=cwd, timeout=timeout)

        try:
            from tools.lazy_deps import ensure as _lazy_ensure
            try:
                _lazy_ensure("terminal.sprites", prompt=False)
            except Exception:
                # 'terminal.sprites' is not (yet) in hermes' LAZY_DEPS
                # allowlist; ensure() is only a convenience installer, so
                # fall through -- the SDK import below is authoritative.
                pass
        except ImportError:
            pass

        from sprites import SpritesClient
        from sprites.exceptions import NotFoundError, SpriteError

        self._NotFoundError = NotFoundError
        self._SpriteError = SpriteError

        from agent.secret_scope import get_secret
        token = get_secret("SPRITES_TOKEN") or get_secret("SPRITE_TOKEN")
        if not token:
            raise ValueError(
                "Sprites backend requires SPRITES_TOKEN. "
                "Run `hermes setup terminal` or set SPRITES_TOKEN in .env."
            )
        self._client = SpritesClient(
            token=token,
            timeout=max(30.0, float(timeout)),
        )
        self._persistent = persistent_filesystem
        self._task_id = task_id
        self._lock = threading.Lock()
        self._sprite = None
        self._desired_labels = _resolve_labels(labels, auto_tags, task_id)
        self._provision_script = provision_script
        self._provision_inline = provision_inline
        self._provision_best_effort = provision_best_effort
        self._provision_timeout = provision_timeout
        self._was_created = False

        # Sprites does not yet honor SpriteConfig sizing knobs (cpu / ram /
        # storage / region) — sandboxes get default sizing. We omit SpriteConfig
        # entirely so the wire format stays minimal until the platform exposes
        # these knobs.
        if persistent_filesystem:
            sprite_name = _resolve_sprite_name(task_id)
            self._sprite_name = sprite_name
            try:
                self._sprite = self._client.get_sprite(sprite_name)
                logger.info(
                    "Sprites: resumed existing sprite %s for task %s",
                    self._sprite.name, task_id,
                )
                self._reconcile_labels()
            except NotFoundError:
                # Cross-process first-use race: two processes can both see
                # 404 here; one create wins and the other gets a duplicate-
                # name error. Adopt the winner instead of failing — re-GET
                # the exact deterministic name, and only re-raise the create
                # error if the Sprite genuinely does not exist (a real
                # create failure, not a race).
                try:
                    self._sprite = self._client.create_sprite(
                        sprite_name,
                        labels=self._desired_labels or None,
                    )
                    logger.info(
                        "Sprites: created sprite %s for task %s (labels=%s)",
                        self._sprite.name, task_id, self._desired_labels,
                    )
                    self._was_created = True
                except SpriteError as create_err:
                    try:
                        self._sprite = self._client.get_sprite(sprite_name)
                        logger.info(
                            "Sprites: adopted sprite %s created concurrently "
                            "by another process (task %s)",
                            self._sprite.name, task_id,
                        )
                        self._reconcile_labels()
                        self._was_created = True
                    except NotFoundError:
                        raise create_err
        else:
            # Ephemeral (container_persistent: false): the sandbox must not
            # survive or be shared across sessions (#82731 contract). A
            # deterministic name would let two independent ephemeral runs
            # attach one live VM — and either cleanup would delete it out
            # from under the other — or silently resume a stale survivor of
            # a crashed prior run. Mint a unique name and NEVER adopt: this
            # constructor only ever creates.
            sprite_name = _ephemeral_sprite_name(task_id)
            self._sprite_name = sprite_name
            self._sprite = self._client.create_sprite(
                sprite_name,
                labels=self._desired_labels or None,
            )
            logger.info(
                "Sprites: created ephemeral sprite %s for task %s (labels=%s)",
                self._sprite.name, task_id, self._desired_labels,
            )
            self._was_created = True

        # Detect remote home dir for .hermes sync target.
        self._remote_home = "/root"
        try:
            from sprites.exceptions import ExitError
            cmd = self._sprite.command("bash", "-c", "echo $HOME", timeout=15)
            home = cmd.combined_output().decode().strip()
            if home:
                self._remote_home = home
                if requested_cwd in {"~", "/root"}:
                    self.cwd = home
        except Exception:
            pass

        self._fs = self._sprite.filesystem("/")
        self._sync_manager = FileSyncManager(
            get_files_fn=lambda: iter_sync_files(f"{self._remote_home}/.hermes"),
            upload_fn=self._sprite_upload,
            delete_fn=self._sprite_delete,
        )
        self._sync_manager.sync(force=True)

        # First-creation provisioning: runs only when the sprite was just
        # created (not resumed). Guarded by a marker file inside the sprite
        # so the adopt-race loser does not double-provision.
        if self._was_created:
            self._provision_sprite()

        self.init_session()

    # ------------------------------------------------------------------
    # File sync callbacks
    # ------------------------------------------------------------------

    def _sprite_upload(self, host_path: str, remote_path: str) -> None:
        """Upload a single file via the SpriteFilesystem API."""
        data = Path(host_path).read_bytes()
        remote = self._fs / remote_path
        remote.parent.mkdir(parents=True, exist_ok=True)
        remote.write_bytes(data)

    def _sprite_delete(self, remote_paths: list[str]) -> None:
        """Delete remote files.

        Missing files are benign (``missing_ok=True``); any OTHER failure
        must propagate so ``FileSyncManager`` rolls back and retries on the
        next cycle. Swallowing errors here would falsely commit the deletion
        — the manager drops the path from its synced set and never retries,
        which can leave stale credential material in a durable Sprite
        permanently.
        """
        for rp in remote_paths:
            (self._fs / rp).unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Label reconciliation
    # ------------------------------------------------------------------

    def _reconcile_labels(self) -> None:
        """Merge desired labels into an existing sprite's label set.

        Called on resume (and on adopt-race) so that labels added to the
        config after a sprite was first created are propagated via
        ``sprite.update(labels=...)``.  Existing labels on the sprite are
        preserved -- this is additive, never destructive.
        """
        if not self._desired_labels:
            return
        try:
            existing = set(self._sprite.labels or [])
        except Exception:
            existing = set()
        desired = set(self._desired_labels)
        missing = desired - existing
        if not missing:
            return
        merged = list(existing | desired)
        try:
            self._sprite = self._sprite.update(labels=merged)
            logger.info(
                "Sprites: reconciled labels on %s: added %s",
                self._sprite.name, sorted(missing),
            )
        except Exception as e:
            logger.warning(
                "Sprites: label reconciliation failed on %s: %s",
                self._sprite_name, e,
            )

    # ------------------------------------------------------------------
    # First-creation provisioning
    # ------------------------------------------------------------------

    _PROVISION_MARKER = ".hermes/.provisioned"

    def _provision_sprite(self) -> None:
        """Run the provisioning script inside the sprite (first creation).

        Only fires when the sprite was just created (``self._was_created``).
        A marker file inside the sprite guards against double-provisioning
        when two processes race on first-use create and one adopts the
        other's sprite.

        Failure policy: fail-fast by default (raise from ``__init__`` so the
        environment never reports ready on a half-provisioned sprite).
        When ``self._provision_best_effort`` is True, failures are logged
        as warnings instead.
        """
        script_path = self._provision_script
        script_inline = self._provision_inline
        if not script_path and not script_inline:
            return

        # Guard: marker file means another process already provisioned.
        marker_path = f"{self._remote_home}/{self._PROVISION_MARKER}"
        try:
            (self._fs / marker_path.lstrip("/")).read_text()
            logger.info(
                "Sprites: provision marker found on %s, skipping",
                self._sprite.name,
            )
            return
        except Exception:
            pass  # marker absent -- proceed

        # Resolve script content.
        if script_path:
            try:
                script_content = Path(script_path).read_text()
            except Exception as e:
                msg = f"provision script not readable: {script_path}: {e}"
                if self._provision_best_effort:
                    logger.warning("Sprites: %s", msg)
                    return
                raise RuntimeError(msg) from e
        else:
            script_content = script_inline

        # Upload the script into the sprite.
        remote_script = f"{self._remote_home}/.hermes/provision.sh"
        remote = self._fs / remote_script.lstrip("/")
        remote.parent.mkdir(parents=True, exist_ok=True)
        remote.write_bytes(script_content.encode())

        # Execute the script with a bounded timeout.
        logger.info(
            "Sprites: running provisioning script on %s (timeout=%ss)",
            self._sprite.name, self._provision_timeout,
        )
        from sprites.exceptions import ExitError
        from sprites.exceptions import TimeoutError as SpritesTimeout

        cmd_timeout = float(self._provision_timeout)
        exit_code = 0
        output = b""
        try:
            cmd = self._sprite.command(
                "bash", "-l", remote_script, timeout=cmd_timeout,
            )
            output = cmd.combined_output()
        except ExitError as e:
            buf = (e.stdout or b"") + (e.stderr or b"")
            output = buf
            exit_code = e.exit_code() if callable(getattr(e, "exit_code", None)) else 1
        except SpritesTimeout:
            output = f"provisioning timed out after {cmd_timeout}s\n".encode()
            exit_code = 124

        decoded = output.decode("utf-8", errors="replace")
        if exit_code == 0:
            # Write the marker so future sessions / race-adopters skip.
            try:
                (self._fs / marker_path.lstrip("/")).write_text("provisioned\n")
            except Exception as e:
                logger.warning(
                    "Sprites: could not write provision marker on %s: %s",
                    self._sprite.name, e,
                )
            logger.info(
                "Sprites: provisioning completed on %s",
                self._sprite.name,
            )
            if decoded.strip():
                logger.debug("Sprites: provision output:\n%s", decoded)
        else:
            msg = (
                f"provisioning script failed on {self._sprite.name} "
                f"(exit {exit_code}):\n{decoded}"
            )
            if self._provision_best_effort:
                logger.warning("Sprites: %s", msg)
            else:
                raise RuntimeError(msg)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _before_execute(self) -> None:
        self._sync_manager.sync()

    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120,
                  stdin_data: str | None = None):
        """Return a _ThreadedProcessHandle wrapping a blocking SDK call."""
        sprite = self._sprite
        from sprites.exceptions import ExitError, TimeoutError as SpritesTimeout

        if login:
            shell_cmd = ["bash", "-l", "-c", cmd_string]
        else:
            shell_cmd = ["bash", "-c", cmd_string]

        # The SDK timeout cancels the WebSocket cleanly, so prefer it over
        # the shell-level ``timeout`` wrapper used by other backends. Never
        # pass None: the SDK has no kill hook on a running Cmd
        # (cancel_fn=None below), so an unbounded exec in a persistent VM
        # could run — and bill — forever. Absent/nonpositive timeouts get a
        # generous fallback deadline (explicit positive values pass through).
        cmd_timeout = float(timeout) if timeout and timeout > 0 else 3600.0

        def exec_fn() -> tuple[str, int]:
            cmd = sprite.command(*shell_cmd, timeout=cmd_timeout)
            try:
                output = cmd.combined_output()
                return (output.decode("utf-8", errors="replace"), 0)
            except ExitError as e:
                # ``e.stdout`` carries the combined output when raised from
                # combined_output(); ``e.stderr`` is empty in that path.
                buf = (e.stdout or b"") + (e.stderr or b"")
                return (buf.decode("utf-8", errors="replace"),
                        e.exit_code() if callable(getattr(e, "exit_code", None)) else 1)
            except SpritesTimeout:
                return (f"command timed out after {cmd_timeout}s\n", 124)

        # No external cancel: the SDK does not expose a kill hook on a
        # running Cmd. The deadline above is the cancellation path.
        return _ThreadedProcessHandle(exec_fn, cancel_fn=None)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self):
        with self._lock:
            if self._sprite is None:
                return

            # No sync_back: the Sprite's persistent ext4 filesystem IS the
            # authoritative store. Files the agent touched stay in the Sprite
            # and are visible again on the next session that resumes by the
            # same task_id. (For ephemeral runs with persistent=False, the
            # Sprite is intentionally deleted with its filesystem.)

            try:
                if self._persistent:
                    logger.info(
                        "Sprites: leaving sprite %s running (persistent)",
                        self._sprite.name,
                    )
                else:
                    self._sprite.delete()
                    logger.info("Sprites: deleted sprite %s", self._sprite.name)
            except Exception as e:
                logger.warning(
                    "Sprites: cleanup failed for %s: %s", self._sprite_name, e
                )
            finally:
                try:
                    self._client.close()
                except Exception:
                    pass
            self._sprite = None