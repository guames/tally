#!/usr/bin/env bash
# sysclaude installer — sets up the unified menu bar widget.
set -euo pipefail

PLUGIN_DIR="${SYSCLAUDE_PLUGIN_DIR:-$HOME/.config/swiftbar-plugins}"
SRC="$(cd "$(dirname "$0")" && pwd)/sysclaude.5s.py"

echo "==> Python deps (for /usr/bin/python3)"
/usr/bin/python3 -m pip install --user -q psutil cryptography curl_cffi

echo "==> macmon (Apple Silicon temperature, no sudo)"
command -v macmon >/dev/null 2>&1 || brew install macmon

echo "==> SwiftBar (menu bar host)"
[ -d "/Applications/SwiftBar.app" ] || brew install --cask swiftbar

echo "==> Installing plugin into $PLUGIN_DIR"
mkdir -p "$PLUGIN_DIR"
cp "$SRC" "$PLUGIN_DIR/sysclaude.5s.py"
chmod +x "$PLUGIN_DIR/sysclaude.5s.py"

echo "==> Pointing SwiftBar at the plugin folder"
defaults write com.ameba.SwiftBar PluginDirectory "$PLUGIN_DIR" || true
defaults write com.ameba.SwiftBar DisableBundleUpdate -bool YES || true

echo "==> Launching SwiftBar"
open -a SwiftBar || true

cat <<'EOF'

Done. Notes:
- On first run macOS will ask to allow access to the "Claude Safe Storage"
  keychain item — click "Always Allow". This lets the widget read your local
  claude.ai session to show plan usage. Nothing leaves your machine.
- The Claude desktop app (or a browser logged into claude.ai) must have been
  signed in at least once, so the session cookie exists locally.
- Change the refresh interval by renaming the file (e.g. sysclaude.10s.py).
EOF
