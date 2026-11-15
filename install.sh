#!/usr/bin/env bash
# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
#
# Install scrawl into a project-local virtual environment, create a launcher
# in ~/.local/bin, and make sure the split config files exist. Re-run this
# script after moving the project.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"

if ! "$PYTHON" -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' 2>/dev/null; then
    echo "error: GTK3 Python bindings are required." >&2
    echo "       On Arch Linux: sudo pacman -S gtk3 python-gobject" >&2
    exit 1
fi

echo "Creating venv in $HERE/.venv ..."
"$PYTHON" -m venv --system-site-packages "$HERE/.venv"
"$HERE/.venv/bin/python" -m pip install --quiet --upgrade pip
"$HERE/.venv/bin/python" -m pip install --quiet pycairo

chmod +x "$HERE/scrawl.py"
mkdir -p "$HOME/.local/bin"

cat > "$HOME/.local/bin/scrawl" <<EOF
#!/usr/bin/env bash
# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
exec "$HERE/.venv/bin/python" "$HERE/scrawl.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/scrawl"

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/scrawl"
echo "Ensuring config files in $CONFIG_DIR ..."
"$HOME/.local/bin/scrawl" config --init

echo
echo "Installed:"
echo "  program:  $HERE/scrawl.py"
echo "  venv:     $HERE/.venv"
echo "  launcher: $HOME/.local/bin/scrawl"
echo "  config:   $CONFIG_DIR"
echo
echo "Try: scrawl pen"
echo "     scrawl set pen-color neon && scrawl set pen-size bold && scrawl pen"
