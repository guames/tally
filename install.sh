#!/usr/bin/env bash
# tally installer — sets up the customizable menu bar dashboard.
set -euo pipefail

PLUGIN_DIR="${TALLY_PLUGIN_DIR:-$HOME/.config/swiftbar-plugins}"
REPO="${TALLY_REPO:-guames/tally}"
REF="${TALLY_REF:-main}"

# Locate the plugin: prefer a local copy (running from a clone), otherwise
# download it so `curl -fsSL .../install.sh | bash` works without cloning.
# Piped to bash, $0 is "bash" and there's no local file -> we fetch instead.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/tally.5s.py" ]; then
  SRC="$SELF_DIR/tally.5s.py"
  echo "==> Using local plugin ($SRC)"
else
  SRC="$(mktemp -t tally.5s.py)"
  trap 'rm -f "$SRC"' EXIT
  URL="https://raw.githubusercontent.com/$REPO/$REF/tally.5s.py"
  echo "==> Downloading plugin from $URL"
  curl -fsSL "$URL" -o "$SRC"
  head -n1 "$SRC" | grep -q '^#!/usr/bin/python3' \
    || { echo "error: downloaded plugin looks wrong (got $(head -c 64 "$SRC"))" >&2; exit 1; }
fi

echo "==> Python deps (for /usr/bin/python3)"
/usr/bin/python3 -m pip install --user -q psutil cryptography curl_cffi Pillow

echo "==> macmon (Apple Silicon temperature, no sudo)"
command -v macmon >/dev/null 2>&1 || brew install macmon

echo "==> SwiftBar (menu bar host)"
[ -d "/Applications/SwiftBar.app" ] || brew install --cask swiftbar

echo "==> Installing plugin into $PLUGIN_DIR"
mkdir -p "$PLUGIN_DIR"
cp "$SRC" "$PLUGIN_DIR/tally.5s.py"
chmod +x "$PLUGIN_DIR/tally.5s.py"

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
- Change the refresh interval by renaming the file (e.g. tally.10s.py).
EOF
