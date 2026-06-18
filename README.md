# sysclaude

A single macOS **menu bar** widget that unifies, in one item:

- **Claude Pro/Max plan usage** — 5‑hour session % and 7‑day weekly %, with reset times (the same numbers as *Settings → Usage* in the Claude app)
- **System** — RAM %, CPU %, and CPU/GPU **temperature**

```
  S76% W68% · 🧠32% 🌡️37°
```

`S` = Claude 5‑hour session · `W` = Claude weekly (all models) · 🧠 = RAM used · 🌡️ = CPU temp.
Clicking the item opens a breakdown with reset times, per‑model weekly usage, and full system stats.

Built as a [SwiftBar](https://github.com/swiftbar/SwiftBar) plugin — no compiled app, just one Python script you can read and audit.

## Why

Existing tools split the job: some show your Claude plan limits, others show system stats. This is **one** menu bar item for both — handy on Apple Silicon laptops where you're watching RAM/heat *and* your Claude session/weekly limits at the same time.

## How it works

| Metric | Source |
|---|---|
| RAM / CPU | [`psutil`](https://github.com/giampaolo/psutil) |
| CPU/GPU temperature | [`macmon`](https://github.com/vladkens/macmon) — reads Apple Silicon sensors **without sudo** |
| Claude plan usage | the claude.ai session cookie from the **Claude desktop app's local cookie store**, used to call the same usage endpoint the web app uses |

For the Claude usage it:
1. Reads the encrypted `sessionKey` + `lastActiveOrg` cookies from the Claude desktop app's local `Cookies` store.
2. Decrypts them with the macOS Keychain key **"Claude Safe Storage"** (the standard Chromium cookie scheme: PBKDF2‑SHA1 + AES‑128‑CBC).
3. Calls `GET https://claude.ai/api/organizations/<org>/usage` with a **Chrome‑impersonated TLS fingerprint** ([`curl_cffi`](https://github.com/yifeikong/curl_cffi)) — plain HTTP clients are blocked by Cloudflare, which checks the TLS/JA3 fingerprint.
4. Reads `five_hour.utilization` (session) and `seven_day.utilization` (weekly).

The result is cached for 60s so the menu bar (which refreshes every 5s for system stats) doesn't hammer the endpoint.

## 🔒 Privacy & security

- **Everything stays on your machine.** The widget talks only to `claude.ai` (the same host the app uses) to read *your own* usage. No third‑party servers, no telemetry.
- **Your session key is never stored or transmitted anywhere else.** It's read from the local cookie store, used in‑memory for the request, and discarded.
- **Nothing sensitive is written to disk.** Only the usage **percentages and reset times** are cached (`/tmp/sysclaude_usage.json`) — no cookies, no session key, no org id.
- macOS will prompt once to allow Keychain access ("Claude Safe Storage") — that's what lets the widget decrypt the cookie. Click **Always Allow**.
- This uses an **undocumented** claude.ai endpoint. It can change or break at any time; if Anthropic changes it, the Claude section degrades gracefully (system stats keep working). Use at your own discretion and in line with Anthropic's terms.

## Requirements

- macOS on **Apple Silicon** (for `macmon` temperature)
- [SwiftBar](https://github.com/swiftbar/SwiftBar)
- The **Claude desktop app** (or a browser logged into claude.ai) signed in at least once
- Python 3 with `psutil`, `cryptography`, `curl_cffi`

## Install

```sh
git clone https://github.com/<you>/sysclaude.git
cd sysclaude
./install.sh
```

The installer sets up the Python deps, `macmon`, SwiftBar, copies the plugin, points SwiftBar at the plugin folder, and launches it. On first run, approve the Keychain prompt.

### Manual

```sh
/usr/bin/python3 -m pip install --user psutil cryptography curl_cffi
brew install macmon
brew install --cask swiftbar
cp sysclaude.5s.py ~/.config/swiftbar-plugins/
defaults write com.ameba.SwiftBar PluginDirectory "$HOME/.config/swiftbar-plugins"
open -a SwiftBar
```

## Configuration

Edit the top of `sysclaude.5s.py`:

- `USAGE_TTL` — how often (seconds) to refresh Claude usage (default 60)
- `WARN`, `CRIT` — % thresholds that turn the numbers orange / red (default 80 / 92)

Refresh cadence of the whole widget is encoded in the filename: `sysclaude.5s.py` = every 5 s. Rename to `.10s.py`, `.30s.py`, etc.

## Troubleshooting

- **Claude section says "indisponível"** — make sure the Claude desktop app has been logged in (so the cookie exists), and that you approved the Keychain prompt. An expired session shows the same; open the app to refresh it.
- **Temperature shows "indisponível"** — `macmon` isn't installed or isn't at `/opt/homebrew/bin/macmon`.
- **Keychain prompt keeps appearing** — click **Always Allow** (not just Allow); the ACL is tied to `/usr/bin/python3`.
- **Nothing in the menu bar** — open SwiftBar and confirm the plugin folder is set to where `sysclaude.5s.py` lives.

## Credits

Inspired by [ClaudeMeter](https://github.com/eddmann/ClaudeMeter) (Claude usage) and [macmon](https://github.com/vladkens/macmon) (sensors). MIT licensed.
