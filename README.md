# hermes-plugin-sprites-provision

Sprites terminal backend for [Hermes Agent](https://github.com/NousResearch/hermes-agent) - run agent shell commands in [Sprites](https://sprites.dev), stateful cloud sandboxes on Fly.io with checkpoint & restore.

Built on top of `NousResearch/hermes-plugin-sprites` (with PR #3 compat fixes), this plugin adds:

1. **Sprite tagging** - attach labels to sprites at creation time and reconcile them on resume via `sprite.update(labels=...)`.
2. **First-creation provisioning** - run a script inside the sprite on first creation only (never on resume), guarded by a marker file for race-safe idempotency.
3. **Nix flake devshell** - reproducible tooling via `nix develop`.

## Install

```bash
# 1. Clone into the Hermes plugins dir
git clone <this-repo> ~/.hermes/plugins/sprites

# 2. Install the SDK (v0.7+ required for labels/update support)
pip install 'sprites-py>=0.7.0,<0.8'

# 3. Enable + select
hermes plugins enable sprites
hermes config set terminal.backend sprites

# 4. Token (get one at https://sprites.dev/account)
echo 'SPRITES_TOKEN=...' >> ~/.hermes/.env
```

## Configuration

All plugin-specific settings live under the `terminal.sprites` section of `config.yaml`:

```yaml
terminal:
  backend: sprites
  sprites:
    # --- Tagging ---
    tags: ["prod", "web"]         # static labels attached to every sprite
    auto_tags: true               # auto-derive: hermes, task-{slug}, profile-{slug}

    # --- First-creation provisioning ---
    provision_script: /path/to/provision.sh   # host-side script to run on first creation
    # provision_inline: |                      # ...or inline script
    #   echo "provisioning..."
    #   apt-get install -y git
    provision_best_effort: false  # false = fail-fast (default), true = log warning on failure
    provision_timeout: 600        # seconds (default 600)
```

### Tagging

Labels are attached at sprite creation time via the sprites-py SDK `labels` parameter. On resume (existing sprite), the plugin reconciles labels additively - missing desired labels are merged into the sprite's existing set via `sprite.update(labels=...)`. Existing labels are never removed.

When `auto_tags: true`, the following labels are auto-derived:
- `hermes` - always added
- `task-{slug}` - the task_id slugified (skipped for `default`)
- `profile-{slug}` - the active profile name (skipped on the default profile)

### Provisioning

The provisioning script runs only on first creation (never on resume). It is uploaded to the sprite's filesystem and executed with `bash -l -c`. A `.provisioned` marker file inside the sprite guards against double-provisioning when two processes race on first-use create (the adopt-race path).

Failure policy:
- Fail-fast (default): non-zero exit raises RuntimeError from `__init__`, so the environment never reports ready on a half-provisioned sprite.
- Best-effort (`provision_best_effort: true`): failures logged as warnings, environment continues initializing.

A default `provision.sh` is included that installs common development tools (git, curl, build-essential).

## Development

### Nix devshell

```bash
nix develop                    # enter the devshell
nix develop -c pytest tests/  # run tests
nix develop -c ruff check .   # lint
```

The devshell provides Python 3.13, pytest, ruff, black, the sprites-py SDK, and hermes-agent source on PYTHONPATH (for import resolution). The sprites-py SDK is installed via `pip --no-deps --target .nix-pkgs/` (not in nixpkgs).

### Tests

```bash
# Unit tests (no token needed)
nix develop -c pytest tests/

# Live integration (needs SPRITES_TOKEN)
nix develop -c pytest tests/ -m integration
```

## Attribution

The Sprites environment was authored by Kyle McLaren (@kylemclaren, Fly.io) as hermes-agent PR #30112 and hardened through PR #93523. Extracted to a standalone plugin by Nous Research. Tagging, provisioning, and Nix devshell added in this fork.

## License

MIT