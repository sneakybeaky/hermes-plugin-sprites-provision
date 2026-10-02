"""Live integration tests for the sprites plugin: tagging + provisioning.

These tests create real sprites, verify label round-trip and provisioning
execution, then destroy them.  They require SPRITES_TOKEN.

Run:
    nix develop -c pytest tests/test_sprites_integration.py -m integration -v
"""
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.integration

# Skip the entire module if no token is available.
_TOKEN = os.environ.get("SPRITES_TOKEN") or os.environ.get("SPRITE_TOKEN")
if not _TOKEN:
    pytest.skip("SPRITES_TOKEN not set - skipping live integration tests", allow_module_level=True)

from sprites import SpritesClient
from sprites.exceptions import NotFoundError


@pytest.fixture
def client():
    c = SpritesClient(token=_TOKEN, timeout=60)
    yield c
    c.close()


@pytest.fixture
def unique_name():
    """Run-unique sprite name so concurrent test runs never collide."""
    return f"hermes-test-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def cleanup_sprite(client, unique_name):
    """Ensure the sprite is destroyed after the test, even on failure."""
    created = []
    yield created
    for name in created:
        try:
            sprite = client.get_sprite(name)
            sprite.destroy()
        except NotFoundError:
            pass
        except Exception as e:
            print(f"cleanup: could not destroy {name}: {e}")


# ------------------------------------------------------------------ #
#  Tagging                                                            #
# ------------------------------------------------------------------ #

class TestTaggingIntegration:
    """Verify labels round-trip through the sprites API."""

    def test_create_with_labels(self, client, unique_name, cleanup_sprite):
        labels = ["prod", "web", "hermes-test"]
        sprite = client.create_sprite(unique_name, labels=labels)
        cleanup_sprite.append(unique_name)

        refetched = client.get_sprite(unique_name)
        assert set(refetched.labels) == set(labels), \
            f"labels mismatch: expected {labels}, got {refetched.labels}"

    def test_update_adds_labels(self, client, unique_name, cleanup_sprite):
        sprite = client.create_sprite(unique_name, labels=["initial"])
        cleanup_sprite.append(unique_name)

        updated = sprite.update(labels=["initial", "added-tag"])
        assert "added-tag" in updated.labels

        refetched = client.get_sprite(unique_name)
        assert "added-tag" in refetched.labels

    def test_create_without_labels(self, client, unique_name, cleanup_sprite):
        sprite = client.create_sprite(unique_name)
        cleanup_sprite.append(unique_name)

        refetched = client.get_sprite(unique_name)
        assert refetched.labels is not None


# ------------------------------------------------------------------ #
#  Provisioning via SpritesEnvironment                                #
# ------------------------------------------------------------------ #

class TestProvisioningIntegration:
    """Verify _provision_sprite runs on first creation and writes the marker."""

    def _make_env(self, unique_name, **prov_kwargs):
        from sprites_environment import SpritesEnvironment
        from sprites.exceptions import SpriteError
        env = SpritesEnvironment.__new__(SpritesEnvironment)
        env._provision_script = prov_kwargs.get("provision_script")
        env._provision_inline = prov_kwargs.get("provision_inline")
        env._provision_best_effort = prov_kwargs.get("provision_best_effort", False)
        env._provision_timeout = prov_kwargs.get("provision_timeout", 120)
        env._remote_home = "/root"
        env._sprite_name = unique_name
        env._was_created = True

        client = SpritesClient(token=_TOKEN, timeout=60)
        # Detect real $HOME (sprites use /home/sprite, not /root).
        try:
            from sprites.exceptions import SpriteError as _SE
            home_cmd = client.get_sprite(unique_name).command(
                "bash", "-c", "echo $HOME", timeout=15,
            )
            home = home_cmd.combined_output().decode().strip()
            if home:
                env._remote_home = home
        except Exception:
            pass
        # If the sprite already exists (409), get it instead of creating.
        try:
            env._sprite = client.create_sprite(unique_name)
        except SpriteError:
            env._sprite = client.get_sprite(unique_name)
        env._fs = env._sprite.filesystem("/")
        env._client = client
        return env

    def test_inline_provision_runs_and_writes_marker(self, client, unique_name, cleanup_sprite):
        cleanup_sprite.append(unique_name)

        env = self._make_env(
            unique_name,
            provision_inline='echo "provisioning ran" > /root/.hermes/provision-test-marker',
        )
        env._provision_sprite()

        cmd = env._sprite.command("bash", "-c", "cat /root/.hermes/provision-test-marker", timeout=15)
        output = cmd.combined_output().decode().strip()
        assert "provisioning ran" in output, f"provision output: {output}"

        marker_cmd = env._sprite.command("bash", "-c", "cat /root/.hermes/.provisioned", timeout=10)
        marker = marker_cmd.combined_output().decode().strip()
        assert "provisioned" in marker

        env._client.close()

    def test_marker_skips_second_provision(self, client, unique_name, cleanup_sprite):
        cleanup_sprite.append(unique_name)

        env = self._make_env(
            unique_name,
            provision_inline='mkdir -p $HOME/.hermes && echo "SHOULD NOT RUN" > $HOME/.hermes/should-not-exist',
        )
        env._provision_sprite()

        # Verify the marker was written.
        marker_path = f"{env._remote_home}/{env._PROVISION_MARKER}"
        marker = (env._fs / marker_path.lstrip("/")).read_text()
        assert "provisioned" in marker, f"marker not written: {marker}"

        # Second run on the SAME env (same _fs handle): marker present, should skip.
        env._provision_sprite()

        # The should-not-exist file should NOT be there because the second
        # provision was skipped by the marker.
        cmd = env._sprite.command(
            "bash", "-c", f"test -f {env._remote_home}/.hermes/should-not-exist && echo EXISTS || echo ABSENT",
            timeout=10,
        )
        result = cmd.combined_output().decode().strip()
        assert "ABSENT" in result, f"second provision was not skipped: {result}"

        env._client.close()

    def test_fail_fast_on_bad_script(self, client, unique_name, cleanup_sprite):
        cleanup_sprite.append(unique_name)

        env = self._make_env(unique_name, provision_inline="exit 42")
        with pytest.raises(RuntimeError, match="provisioning script failed"):
            env._provision_sprite()

        env._client.close()

    def test_best_effort_logs_failure(self, client, unique_name, cleanup_sprite):
        cleanup_sprite.append(unique_name)

        env = self._make_env(
            unique_name,
            provision_inline="exit 42",
            provision_best_effort=True,
        )
        env._provision_sprite()  # should not raise

        env._client.close()


# ------------------------------------------------------------------ #
#  End-to-end: SpritesEnvironment with tags + provisioning             #
# ------------------------------------------------------------------ #

class TestEndToEndIntegration:
    """Full SpritesEnvironment lifecycle: create with tags, provision, resume."""

    def test_create_with_tags_and_provision(self, client, unique_name, cleanup_sprite):
        from sprites_environment import SpritesEnvironment

        cleanup_sprite.append(unique_name)

        with patch("sprites_environment._resolve_sprite_name", return_value=unique_name):
            with patch("sprites_environment._resolve_profile_identity", return_value=None):
                env = SpritesEnvironment(
                    cwd="/root",
                    timeout=120,
                    persistent_filesystem=True,
                    task_id=unique_name,
                    labels=["e2e-test", "hermes"],
                    auto_tags=False,
                    provision_inline='mkdir -p $HOME/.hermes && echo "e2e provision ok" > $HOME/.hermes/e2e-marker',
                    provision_timeout=120,
                )

        try:
            refetched = client.get_sprite(unique_name)
            assert "e2e-test" in refetched.labels, f"labels: {refetched.labels}"
            assert "hermes" in refetched.labels

            cmd = env._sprite.command("bash", "-c", f"cat {env._remote_home}/.hermes/e2e-marker", timeout=15)
            output = cmd.combined_output().decode().strip()
            assert "e2e provision ok" in output, f"provision output: {output}"
        finally:
            env.cleanup()
            refetched = client.get_sprite(unique_name)
            assert refetched is not None


# ------------------------------------------------------------------ #
#  Nix bootstrap provision                                            #
# ------------------------------------------------------------------ #

class TestNixBootstrapIntegration:
    """Provision a sprite with the Nix installer and verify it works.

    Exercises the .hermes.md bootstrap procedure end-to-end: install
    Determinate Nix with --init none, fix /nix ownership, and verify
    `nix --version` runs inside the sprite.
    """

    NIX_INSTALL_SCRIPT = """set -euo pipefail

# Install Determinate Nix (single-user, no daemon — sprites have no systemd).
curl -fsSL https://install.determinate.systems/nix | sh -s -- install linux --init none --no-confirm

# The installer runs as root via sudo; fix ownership so the sprite user owns /nix.
sudo chown -R "$(whoami)" /nix

# Put nix on PATH for this shell and persist for fish.
export PATH="/nix/var/nix/profiles/default/bin:$PATH"
echo 'set -x PATH /nix/var/nix/profiles/default/bin $PATH' >> ~/.config/fish/config.fish

# Verify.
nix --version
"""

    def test_nix_installs_and_runs(self, client, unique_name, cleanup_sprite):
        from sprites_environment import SpritesEnvironment

        cleanup_sprite.append(unique_name)

        with patch("sprites_environment._resolve_sprite_name", return_value=unique_name):
            with patch("sprites_environment._resolve_profile_identity", return_value=None):
                env = SpritesEnvironment(
                    cwd="/root",
                    timeout=300,
                    persistent_filesystem=True,
                    task_id=unique_name,
                    labels=["nix-test", "hermes"],
                    auto_tags=False,
                    provision_inline=self.NIX_INSTALL_SCRIPT,
                    provision_best_effort=False,
                    provision_timeout=300,
                )

        try:
            # Provisioning ran during __init__; verify nix is available.
            cmd = env._sprite.command(
                "bash", "-lc",
                "export PATH=/nix/var/nix/profiles/default/bin:$PATH && nix --version",
                timeout=30,
            )
            output = cmd.combined_output().decode().strip()
            assert "nix" in output.lower(), f"nix --version output: {output}"

            # Verify the fish config line was appended.
            cmd = env._sprite.command(
                "bash", "-c",
                "grep 'nix/var/nix/profiles/default/bin' ~/.config/fish/config.fish",
                timeout=10,
            )
            fish_output = cmd.combined_output().decode().strip()
            assert "nix/var/nix/profiles/default/bin" in fish_output, \
                f"fish config not updated: {fish_output}"

            # Verify the .provisioned marker exists (provisioning completed).
            marker_cmd = env._sprite.command(
                "bash", "-c", f"cat {env._remote_home}/.hermes/.provisioned", timeout=10,
            )
            marker = marker_cmd.combined_output().decode().strip()
            assert "provisioned" in marker
        finally:
            env.cleanup()