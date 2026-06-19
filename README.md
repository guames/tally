# Tally

A customizable macOS **menu bar dashboard**, built as a single, auditable
[SwiftBar](https://github.com/swiftbar/SwiftBar) plugin — one Python script, no
compiled app. It puts the metrics you care about into one menu bar item and is
meant to **grow** as you add widgets.

Today it ships three:

- **Claude** — Pro/Max plan usage: 5-hour **session** % and 7-day **weekly** %,
  with reset times (the same numbers as *Settings → Usage* in the Claude app).
- **System** — RAM, CPU, and **temperature**.
- **Ember** — control a local [Ember](https://github.com/guames/ember) MLX
  inference router: see the current model, warm/unload models, open status.

```
menu bar:   [S 17%] [W 53%]   10.4/24GB   🌡 37°
```

`S` = Claude session · `W` = Claude weekly. The S/W bars are rendered as slim,
monochrome **battery-style glyphs** (the % inside) so they blend with the native
menu bar icons. Click the item for a full breakdown — where the numbers turn
orange/red as usage climbs — plus the Ember controls.

> 📐 See **[SPEC.md](SPEC.md)** for the full specification (this project follows
> specification-driven development — the spec is the source of truth).

## Why

Existing tools split the job: some show your Claude plan limits, others show
system stats. Tally is **one** menu bar item for all of it — handy on Apple
Silicon laptops where you're watching RAM/heat, your Claude session/weekly
limits, and your local LLM router at the same time.

## How it works

| Widget | Source |
|---|---|
| RAM / CPU | [`psutil`](https://github.com/giampaolo/psutil) |
| Temperature | [`macmon`](https://github.com/vladkens/macmon) — Apple Silicon sensors, **no sudo** |
| Claude usage | the claude.ai session cookie from the **Claude desktop app's local cookie store**, used to call the same usage endpoint the web app uses |
| Ember | HTTP to the local router (`127.0.0.1:8000`) |

For Claude usage it:

1. Reads the encrypted `sessionKey` + `lastActiveOrg` cookies from the Claude
   desktop app's local `Cookies` store.
2. Decrypts them with the macOS Keychain key **"Claude Safe Storage"** (the
   standard Chromium cookie scheme: PBKDF2-SHA1 + AES-128-CBC).
3. Calls `GET https://claude.ai/api/organizations/<org>/usage` with a
   **Chrome-impersonated TLS fingerprint** ([`curl_cffi`](https://github.com/yifeikong/curl_cffi))
   — plain HTTP clients are blocked by Cloudflare, which checks the TLS/JA3
   fingerprint.
4. Reads `five_hour.utilization` (session) and `seven_day.utilization` (weekly).

The network call **never runs inline.** The render returns the cached value
instantly and, if it's older than `USAGE_TTL` (120 s), kicks off a detached
background worker to refresh it — so a slow claude.ai never freezes the menu bar.

## 🔒 Privacy & security

- **Everything stays on your machine.** Tally talks only to `claude.ai` (the same
  host the app uses) to read *your own* usage, and to your local Ember router. No
  third-party servers, no telemetry.
- **Your session key is never stored or transmitted anywhere else.** It's read from
  the local cookie store, used in-memory for the request, and discarded.
- **Nothing sensitive is written to disk.** Only the usage **percentages and reset
  times** are cached (`/tmp/tally_usage.json`) — no cookies, no session key, no org id.
- macOS prompts once to allow Keychain access ("Claude Safe Storage"). Click
  **Always Allow** — that's what lets the widget decrypt the cookie.
- This uses an **undocumented** claude.ai endpoint. It can change or break at any
  time; if it does, the Claude section degrades gracefully (the rest keeps
  working). Use at your discretion and in line with Anthropic's terms.

## Requirements

- macOS on **Apple Silicon** (for `macmon` temperature)
- [SwiftBar](https://github.com/swiftbar/SwiftBar)
- The **Claude desktop app** (or a browser logged into claude.ai) signed in at least once
- Python 3 with `psutil`, `cryptography`, `curl_cffi`, `Pillow`
- *(optional)* a running [Ember](https://github.com/guames/ember) router for the Ember widget

## Install

```sh
git clone https://github.com/guames/tally.git
cd tally
./install.sh
```

The installer sets up the Python deps, `macmon`, SwiftBar, copies the plugin,
points SwiftBar at the plugin folder, and launches it. On first run, approve the
Keychain prompt.

### Manual

```sh
/usr/bin/python3 -m pip install --user psutil cryptography curl_cffi Pillow
brew install macmon
brew install --cask swiftbar
cp tally.5s.py ~/.config/swiftbar-plugins/
defaults write com.ameba.SwiftBar PluginDirectory "$HOME/.config/swiftbar-plugins"
open -a SwiftBar
```

## Configuration

Edit the top of `tally.5s.py`:

- `USAGE_TTL` — max staleness (seconds) of the Claude cache before a background
  refresh (default 120)
- `GREEN_MAX`, `YELLOW_MAX` — % thresholds that turn the **dropdown** text orange /
  red (default 50 / 85); the menu bar bars themselves are monochrome
- `BATT_OUTLINE`, `BATT_FILL`, `MENUBAR_PILL_H` — look of the slim battery-style bars
- `EMBER_URL`, `EMBER_BIN` — Ember router base URL and CLI (or set
  `MLX_ROUTER_HOST` / `MLX_ROUTER_PORT`)

The whole widget's refresh cadence is encoded in the filename: `tally.5s.py` =
every 5 s. Rename to `.10s.py`, `.30s.py`, etc.

Without `Pillow`, the menu bar falls back to a plain-text/unicode bar — everything
else still works.

## Troubleshooting

- **Claude section says "unavailable"** — make sure the Claude desktop app has been
  logged in (so the cookie exists) and that you approved the Keychain prompt. An
  expired session shows the same; open the app to refresh it.
- **Temperature says "unavailable"** — `macmon` isn't installed or isn't at
  `/opt/homebrew/bin/macmon`.
- **Keychain prompt keeps appearing** — click **Always Allow** (not just Allow);
  the ACL is tied to `/usr/bin/python3`.
- **Ember section says "router offline"** — start your router (`ember serve`, or
  your `mlx_router.py`); Tally reads it over HTTP on `127.0.0.1:8000`.
- **Nothing in the menu bar** — open SwiftBar and confirm the plugin folder is set
  to where `tally.5s.py` lives. After editing the script, a full SwiftBar restart
  (`killall SwiftBar; open -a SwiftBar`) reloads it reliably.

## Contributing

Tally is built widget-by-widget; see **[SPEC.md](SPEC.md) §10** for how to add one.
Issues and PRs welcome.

## Credits

Inspired by [ClaudeMeter](https://github.com/eddmann/ClaudeMeter) (Claude usage)
and [macmon](https://github.com/vladkens/macmon) (sensors). MIT licensed.
