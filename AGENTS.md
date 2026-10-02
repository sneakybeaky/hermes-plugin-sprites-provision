# Install Instructions

This is a Hermes Agent terminal backend plugin that adds tagging and
first-creation provisioning to the Sprites (Fly.io) backend.

## One-command install

```bash
hermes plugins install sneakybeaky/hermes-plugin-sprites-provision --enable
```

## Post-install steps

### 1. Install the sprites-py SDK

The Hermes venv is root-owned and read-only. Install to the
**lazy-packages** target (NOT python-packages) — this is the
`HERMES_LAZY_INSTALL_TARGET` that Hermes reads at startup via
`activate_durable_lazy_target()`. Do NOT use `/opt/data/.hermes/python-packages`
or rely on `PYTHONPATH` in `.env` — the runtime does not read it.

```bash
mkdir -p /opt/data/lazy-packages
uv pip install --python /opt/hermes/.venv/bin/python3 \
  --target /opt/data/lazy-packages \
  --no-deps \
  'sprites-py>=0.7.0,<0.8'
```

`--no-deps` is safe — the SDK's deps (httpx, websockets, anyio, certifi,
etc.) are already in the Hermes venv. Using `--no-deps` avoids duplicate
copies and version conflicts with the venv's exact-pinned deps.

Verify:
```bash
PYTHONPATH=/opt/data/lazy-packages \
  /opt/hermes/.venv/bin/python3 -c "import sprites; print('OK')"
```

**A restart is required** — the running Hermes process only reads
`/opt/data/lazy-packages` at startup via `activate_durable_lazy_target()`.
Restart from the Nous portal (or start a new session) after installing.

### 2. Set the token

```bash
echo 'SPRITES_TOKEN=<your-token>' >> /opt/data/.env
```

Get a token at https://sprites.dev/account. A Restricted Token with
`prefix=hermes` is recommended for CI/shared use.

### 3. Set the terminal backend

```bash
hermes config set terminal.backend sprites
```

### 4. (Optional) Configure tagging and provisioning

Add to `config.yaml` (or use `hermes config set`):

```yaml
terminal:
  sprites:
    tags: ["prod", "web"]
    auto_tags: true
    provision_script: /path/to/provision.sh
    # provision_inline: |
    #   echo "provisioning..."
    provision_best_effort: false
    provision_timeout: 600
```

### 5. Verify

```bash
hermes plugins doctor sprites
hermes config get terminal.backend
```

Start a new session for config changes to take effect.