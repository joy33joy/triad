#!/usr/bin/env bash
# Install the `triad` command for your user.
set -euo pipefail
cd "$(dirname "$0")/.."
if command -v uv >/dev/null; then
  uv tool install --force --editable .   # editable: edits to this folder apply immediately
elif command -v pipx >/dev/null; then
  pipx install --force --editable .
else
  python3 -m venv "$HOME/.local/share/triad-venv"
  "$HOME/.local/share/triad-venv/bin/pip" install --upgrade -e .
  mkdir -p "$HOME/.local/bin"
  ln -sf "$HOME/.local/share/triad-venv/bin/triad" "$HOME/.local/bin/triad"
fi
echo
echo "Installed. Try:  triad doctor        (check your three CLIs)"
echo "                 mkdir ~/my-app && cd ~/my-app && triad"
echo "                 triad --demo        (offline demo, no quota used)"
