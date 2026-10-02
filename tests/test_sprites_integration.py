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
        env = SpritesEnvironment.__new__(SpritesEnvironment)
        env._provision_script = prov_kwargs.get("provision_script")
        env._provision_inline = prov_kwargs.get("provision_inline")
        env._provision_best_effort = prov_kwargs.get("provision_best_effort", False)
        env._provision_timeout = prov_kwargs.get("provision_timeout", 120)
        env._remote_home = "/root"
        env._sprite_name = unique_name
        env._was_created = True

        client = SpritesClient(token=_TOKEN, timeout=60)
        env._sprite = client.create_sprite(unique_name)
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
            provision_inline='echo "SHOULD NOT RUN" > /root/.hermes/should-not-exist',
        )
        env._provision_sprite()

        # Second run: marker present, should skip.
        env2 = self._make_env(
            unique_name,
            provision_inline='echo "SHOULD NOT RUN" > /root/.hermes/should-not-exist',
        )
        env2._provision_sprite()

        cmd = env._sprite.command(
            "bash", "-c", "test -f /root/.hermes/should-not-exist && echo EXISTS || echo ABSENT",
            timeout=10,
        )
        result = cmd.combined_output().decode().strip()
        assert "ABSENT" in result, f"second provision was not skipped: {result}"

        env._client.close()
        env2._client.close()

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
                    provision_inline='echo "e2e provision ok" > /root/.hermes/e2e-marker',
                    provision_timeout=120,
                )

        try:
            refetched = client.get_sprite(unique_name)
            assert "e2e-test" in refetched.labels, f"labels: {refetched.labels}"
            assert "hermes" in refetched.labels

            cmd = env._sprite.command("bash", "-c", "cat /root/.hermes/e2e-marker", timeout=15)
            output = cmd.combined_output().decode().strip()
            assert "e2e provision ok" in output, f"provision output: {output}"
        finally:
            env.cleanup()
            refetched = client.get_sprite(unique_name)
            assert refetched is not None