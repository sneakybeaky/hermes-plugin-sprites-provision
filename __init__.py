"""Sprites terminal backend plugin for Hermes Agent.

Registers ``terminal.backend: sprites`` — stateful cloud sandboxes on
Fly.io with checkpoint & restore, persistent by default and resumed by a
deterministic, profile-scoped name.

Install into ``~/.hermes/plugins/sprites/`` and enable via
``hermes plugins enable sprites``, then:

    hermes config set terminal.backend sprites
    # SPRITES_TOKEN in ~/.hermes/.env (get one with `sprite login`)

Attribution: the Sprites environment was authored by Kyle McLaren
(@kylemclaren, Fly.io) as hermes-agent PR #30112 and hardened through the
#93523 salvage review (identity digests, DNS bounds, fail-closed profile
resolution, race-safe first-use create, bounded exec deadlines). Extracted
here when terminal backends became pluggable (hermes-agent PR #94400).
"""

from __future__ import annotations

import importlib.util
import os
from typing import Any, Dict, Optional

from agent.terminal_env_provider import TerminalEnvironmentProvider

_SPRITES_SPEC = "sprites-py>=0.7.0,<0.8"


def _sdk_installed() -> bool:
    """True when the sprites SDK is importable.

    ``find_spec`` raises ValueError for an already-imported module whose
    ``__spec__`` is None (e.g. injected test doubles); treat presence in
    sys.modules as installed.
    """
    import sys

    if "sprites" in sys.modules:
        return True
    try:
        return importlib.util.find_spec("sprites") is not None
    except (ImportError, ValueError):
        return False


def _get_token() -> Optional[str]:
    try:
        from agent.secret_scope import get_secret

        return get_secret("SPRITES_TOKEN") or get_secret("SPRITE_TOKEN")
    except Exception:
        return os.getenv("SPRITES_TOKEN") or os.getenv("SPRITE_TOKEN")


def _normalise_tags(raw):
    """Coerce *raw* (list, JSON string, CSV string, or None) to list-or-None."""
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        import json as _json
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except (ValueError, TypeError):
            pass
        return [t.strip() for t in raw.split(",") if t.strip()]
    return None


class SpritesProvider(TerminalEnvironmentProvider):
    """Sprites — stateful cloud sandboxes on Fly.io."""

    # _plugin_* attributes hold settings read from plugins.entries.sprites.settings.*
    # via ctx.get_config() in register().  None means "not configured via plugin
    # settings" — fall through to terminal.sprites.* for backwards compat.
    _plugin_labels: "list | None" = None
    _plugin_auto_tags: "bool | None" = None
    _plugin_provision_script: "str | None" = None
    _plugin_provision_inline: "str | None" = None
    _plugin_provision_best_effort: "bool | None" = None
    _plugin_provision_timeout: "int | None" = None

    name = "sprites"
    display_name = "Sprites Provisioned"
    is_remote = True
    is_container = True
    # A durable Sprite is resumed BY NAME; under container_persistent: false
    # a shared deterministic name would let two independent ephemeral runs
    # attach one live VM and delete it out from under each other (#82731).
    session_isolated_when_nonpersistent = True

    @property
    def description(self) -> str:
        return (
            "Run commands in a Sprite — a stateful cloud sandbox on Fly.io "
            "with checkpoint & restore."
        )

    @property
    def env_description(self) -> str:
        return "a Sprite — a stateful cloud sandbox on Fly.io (Linux)"

    @property
    def cache_path_base(self) -> Optional[str]:
        # Hermes cache files are synced under the remote user's home.
        return "~/.hermes"

    @property
    def strip_env_keys(self) -> frozenset:
        return frozenset({"SPRITES_TOKEN", "SPRITE_TOKEN"})

    def is_available(self) -> bool:
        return (
            _sdk_installed()
            and bool(_get_token())
        )

    def check_requirements(self, config: Dict[str, Any]) -> bool:
        import logging

        logger = logging.getLogger(__name__)
        if not _sdk_installed():
            logger.error(
                "sprites-py SDK is required. On hosted Hermes (read-only venv), "
                "install to the lazy-packages target (NOT python-packages): "
                "uv pip install --python /opt/hermes/.venv/bin/python3 "
                "--target /opt/data/lazy-packages --no-deps '%s'. "
                "Restart Hermes from the Nous portal so activate_durable_lazy_target() "
                "picks up the new package at startup.",
                _SPRITES_SPEC,
            )
            return False
        if not _get_token():
            logger.error(
                "Sprites backend requires SPRITES_TOKEN. Run `sprite login` "
                "and put the token in ~/.hermes/.env as SPRITES_TOKEN."
            )
            return False
        return True

    def probe(self):
        if not _sdk_installed():
            return (
                "needs_setup",
                f"sprites-py SDK not installed — pip install '{_SPRITES_SPEC}'.",
            )
        if _get_token():
            return ("ready", "")
        return ("needs_setup", "Set SPRITES_TOKEN to use the Sprites backend.")

    def setup_instructions(self):
        return [
            "Stateful cloud sandboxes on Fly.io, with checkpoint & restore.",
            "Sprites persist between sessions and are reused by task_id.",
            "",
            "Install the sprites-py SDK (v0.7+ required for labels/update):",
            "  uv pip install --python /opt/hermes/.venv/bin/python3",
            "    --target /opt/data/lazy-packages --no-deps",
            "    'sprites-py>=0.7.0,<0.8'",
            "  (lazy-packages is the HERMES_LAZY_INSTALL_TARGET read at startup)",
            "  (Do NOT use python-packages or PYTHONPATH in .env -- runtime ignores it)",
            "",
            "Get a token at: https://sprites.dev/account",
            "Tip: mint a Restricted Token with prefix=hermes to scope it to",
            "     hermes-* sprites only. Recommended for CI / shared use.",
            "Save it in /opt/data/.env as SPRITES_TOKEN.",
            "Set backend: hermes config set terminal.backend sprites",
            "",
            "Optional tagging/provisioning config (in config.yaml):",
            "  terminal.sprites.tags: ['prod', 'web']",

            "  terminal.sprites.auto_tags: true",
            "  terminal.sprites.provision_script: /path/to/provision.sh",
            "",
            "Note: Sprites allocates compute dynamically (up to 8 CPU / 16 GB RAM).",
        ]

    def doctor_checks(self):
        rows = []
        token = _get_token()
        rows.append((
            bool(token),
            "Sprites token",
            "(configured)" if token else "(required — run `sprite login`, save as SPRITES_TOKEN)",
        ))
        sdk_ok = _sdk_installed()
        rows.append((
            sdk_ok,
            "sprites-py SDK",
            "(installed)" if sdk_ok else f"(pip install '{_SPRITES_SPEC}')",
        ))
        persistent = os.getenv("TERMINAL_CONTAINER_PERSISTENT", "true").lower() in {"1", "true", "yes", "on"}
        rows.append((
            True,
            "Sprites persistence",
            "Sprite stays alive across sessions; its ext4 filesystem is the authoritative store"
            if persistent else "Sprite is deleted on cleanup (ephemeral)",
        ))
        rows.append((
            True,
            "Sprites tagging",
            "supported (configure via terminal.sprites.tags)",
        ))
        rows.append((
            True,
            "Sprites provisioning",
            "supported (configure via terminal.sprites.provision_script or provision_inline)",
        ))
        return rows

    def create_environment(self, *, cwd, timeout, task_id="default",
                           image=None, container_config=None, **kwargs):
        try:
            # Normal path: loaded by the Hermes plugin manager as a package.
            from .sprites_environment import SpritesEnvironment
        except ImportError:
            # Test / direct-import path (repo root on sys.path).
            from sprites_environment import SpritesEnvironment

        cc = container_config or {}
        # terminal.sprites.* is kept for backwards compatibility.  The
        # authoritative config path is plugins.entries.sprites.settings.*
        # (declared in config_schema and read via ctx.get_config in register()),
        # stored as _plugin_* attributes.  Plugin settings survive config-editor
        # round-trips; terminal.sprites.* may be stripped by the Hermes config
        # editor for keys not in the terminal config schema.
        sc = cc.get("sprites", {})

        # --- labels / tags ---
        # Plugin settings win; fall back to terminal.sprites.tags or the flat
        # sprites_tags key used by older config-bridge versions.
        if self._plugin_labels is not None:
            tags = self._plugin_labels
        else:
            raw_tags = sc["tags"] if "tags" in sc else cc.get("sprites_tags")
            tags = _normalise_tags(raw_tags)

        # --- auto_tags ---
        if self._plugin_auto_tags is not None:
            auto_tags = self._plugin_auto_tags
        else:
            auto_tags = sc.get("auto_tags", cc.get("sprites_auto_tags", False))

        # --- provisioning ---
        provision_script = (
            self._plugin_provision_script
            if self._plugin_provision_script is not None
            else (sc.get("provision_script") or cc.get("sprites_provision_script"))
        )
        provision_inline = (
            self._plugin_provision_inline
            if self._plugin_provision_inline is not None
            else (sc.get("provision_inline") or cc.get("sprites_provision_inline"))
        )
        if self._plugin_provision_best_effort is not None:
            provision_best_effort = self._plugin_provision_best_effort
        else:
            provision_best_effort = sc.get(
                "provision_best_effort", cc.get("sprites_provision_best_effort", False)
            )
        provision_timeout = (
            self._plugin_provision_timeout
            if self._plugin_provision_timeout is not None
            else sc.get("provision_timeout", cc.get("sprites_provision_timeout", 600))
        )

        return SpritesEnvironment(
            cwd=cwd,
            timeout=timeout,
            persistent_filesystem=cc.get("container_persistent", True),
            task_id=task_id,
            labels=tags,
            auto_tags=auto_tags,
            provision_script=provision_script,
            provision_inline=provision_inline,
            provision_best_effort=provision_best_effort,
            provision_timeout=provision_timeout,
        )


def register(ctx):
    provider = SpritesProvider()

    # Read plugin settings from plugins.entries.sprites.settings.* via
    # ctx.get_config().  These are declared in config_schema (plugin.yaml v2)
    # and are preserved by the Hermes config editor; unlike terminal.sprites.*
    # they survive a config round-trip.  Store them on the provider so
    # create_environment() can use them without needing ctx at call time.
    raw_tags = ctx.get_config("tags", default=None)
    provider._plugin_labels = _normalise_tags(raw_tags) if raw_tags is not None else None

    val = ctx.get_config("auto_tags", default=None)
    provider._plugin_auto_tags = bool(val) if val is not None else None

    val = ctx.get_config("provision_script", default=None)
    provider._plugin_provision_script = str(val) if val else None

    val = ctx.get_config("provision_inline", default=None)
    provider._plugin_provision_inline = str(val) if val else None

    val = ctx.get_config("provision_best_effort", default=None)
    provider._plugin_provision_best_effort = bool(val) if val is not None else None

    val = ctx.get_config("provision_timeout", default=None)
    provider._plugin_provision_timeout = int(val) if val is not None else None

    ctx.register_terminal_environment_provider(provider)