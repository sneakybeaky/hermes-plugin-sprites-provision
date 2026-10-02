{
  description = "Hermes Agent Sprites terminal backend plugin — tagging, provisioning, and Nix devshell";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    hermes-agent-src = {
      url = "github:NousResearch/hermes-agent";
      flake = false;
    };
  };

  outputs =
    { self, nixpkgs, flake-utils, hermes-agent-src }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python313;

        # Lightweight deps the plugin's import chain pulls from hermes-agent
        # (we put the source on PYTHONPATH rather than pip-installing the
        # whole exact-pinned hermes-agent tree, which is gated to py3.14).
        hermesRuntimeDeps = with python.pkgs; [
          httpx
          pydantic
          ruamel-yaml-clib
          ruamel-yaml
          python-dotenv
          rich
          tenacity
          packaging
          certifi
          websockets
          psutil
        ];

        devTools = with python.pkgs; [
          pip
          pytest
          pytest-mock
          ruff
          black
        ];
      in
      {
        devShells.default = pkgs.mkShellNoCC {
          packages = [
            python
            pkgs.git
          ];

          # These go into the nix-provided python env so they are available
          # without a separate venv. sprites-py is installed via pip in the
          # shellHook because it is not in nixpkgs.
          buildInputs = hermesRuntimeDeps ++ devTools;

          shellHook = ''
            # Set PYTHONPATH first so the import check below can see a
            # previously-installed sprites-py and skip re-installing.
            export PYTHONPATH="$PWD/.nix-pkgs:${hermes-agent-src.outPath}:$PYTHONPATH"

            # sprites-py is not in nixpkgs. Install it via pip --no-deps
            # --target into a project-local dir that persists on the
            # sprite's ext4. The nix-provided deps (httpx, pydantic, …)
            # are already on the nix python's sys.path, so --no-deps is
            # safe and avoids duplicate copies.
            if ! python -c "import sprites" 2>/dev/null; then
              echo "[devshell] installing sprites-py SDK to .nix-pkgs/..."
              pip install --no-deps --target "$PWD/.nix-pkgs" 'sprites-py>=0.7.0,<0.8' --quiet
            fi

            echo "[devshell] ready — Python $(python --version 2>&1)"
            echo "[devshell] sprites-py + hermes-agent source on PYTHONPATH"
            echo "[devshell] run: pytest tests/  or  ruff check ."
          '';
        };
      });
}