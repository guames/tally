# Tally — Specification

> Specification-Driven Development (SDD): this document is the **source of truth**
> for what Tally does. Code in `tally.5s.py` implements this spec; when behaviour
> and spec disagree, one of them is a bug. Keep this file in sync with changes.

- **Status:** living spec
- **Component:** single SwiftBar plugin (`tally.5s.py`)
- **Target:** macOS on Apple Silicon
- **License:** MIT

---

## 1. Purpose

Tally is a **customizable macOS menu bar dashboard**. It puts a small set of
"widgets" into a single menu bar item and a dropdown, and is designed to **grow**
— new widgets can be added without changing the existing ones.

Today it ships four widgets:

1. **Claude** — Anthropic Pro/Max plan usage (5-hour session and 7-day weekly).
2. **System** — RAM, CPU, temperature.
3. **Ember** — control surface for a local [Ember](https://github.com/guames/ember)
   MLX inference router (current model, warm, unload).
4. **Ledger** — a switch that flips Claude Code between the local
   [Ledger](https://github.com/guames/ledger) proxy and Anthropic-direct, with
   gateway start/stop.

## 2. Goals & non-goals

**Goals**
- One glanceable menu bar item for the metrics the user cares about.
- Auditable: a single readable Python script, no compiled binary.
- Local-only and privacy-preserving (see §8).
- Never block the menu bar UI on the network (see §7).
- Easy to extend with new widgets (see §10).

**Non-goals**
- Not a general system monitor (no graphs/history).
- Not a Claude API cost tracker (Pro/Max usage is a **plan %**, not $/token).
- No cross-platform support (Apple Silicon + macOS only).

## 3. Architecture

- **Host:** [SwiftBar](https://github.com/swiftbar/SwiftBar). SwiftBar runs the
  script on an interval encoded in the filename (`tally.5s.py` ⇒ every 5 s) and
  renders its stdout (SwiftBar plugin format) as the menu bar item + dropdown.
- **Single process per render.** All widgets render in one script invocation, so
  any slow/blocking call freezes the whole UI — hence the non-blocking rules in §7.
- **Self-invocation for actions.** Interactive dropdown items run
  `bash=<abs path to tally.5s.py> param1=<verb> …`. A guard in `__main__`
  detects the leading argument, performs the action, and exits **without**
  printing a menu. Verbs:
  - `fetch-usage` → refresh the Claude cache (background worker).
  - `ember <action> <arg>` → call the Ember router (warm / unload).
  - `ledger <action>` → flip the proxy switch (proxy / direct) or start/stop the gateway.
- **Optional dependency degradation.** Image rendering needs Pillow; if Pillow is
  unavailable, `HAVE_PIL` is false and the menu bar falls back to a unicode bar.

## 4. Functional requirements

### 4.1 Menu bar title

- When Claude usage is available, the title is **one PNG image** (`| image=`)
  containing two slim bars:
  - **S** — Claude 5-hour session %.
  - **W** — Claude weekly %.
  Each bar shows its letter on the left and its **percentage centered** inside.
- After the image, as text: **RAM** as `used/total` GB (1 decimal, e.g. `10.4/24GB`)
  and a single **temperature** as `:thermometer.medium:<n>°` (SF Symbol icon +
  thin space U+2009 + value).
- When Claude usage is unavailable, the title degrades to text: `RAM … ⚙️CPU% 🌡️T°`.
- Strip geometry: total height `MENUBAR_H` (44 px). Bars are `MENUBAR_BAR_H`
  (24 px) tall but the pill is only `MENUBAR_PILL_H` (14 px), vertically centered.
  **The total image must not exceed the menu bar height or SwiftBar hides it.**

### 4.2 Bar rendering (`render_bar` / `_draw_bar`)

- Style: a **macOS-battery glyph** — a light rounded-rectangle **outline** (no
  battery "nub"), a neutral fill up to the % level, and the % centered inside.
- **Monochrome** (`BATT_OUTLINE` / `BATT_FILL`) so the bars blend with the native
  menu bar icons. **S and W look identical** — only the fill level differs; usage
  is *not* colour-coded in the menu bar (the colour clashed with the monochrome bar).
- The **% font size is derived from the image height, not the body thickness**, so
  the body stays slim while the number stays readable; the % is white with a thin
  dark stroke so it reads over both the filled and empty parts.
- Rendered at 4× supersample, downscaled with LANCZOS; font `SFNSRounded.ttf`.

### 4.3 Dropdown

Sections, in order, separated by `---`:

1. **`Claude — Pro`**
   - `S  Session 5h: <p>%  ·  resets <when>`
   - `W  Weekly:    <p>%  ·  resets <when>`
   - optional `Opus 7d <p>%   Sonnet 7d <p>%`
   - text coloured by band (`band_color`: red > `YELLOW_MAX`, orange > `GREEN_MAX`).
   - if usage unavailable: `unavailable (app closed / session expired?)`.
2. **`System`**
   - `RAM:  <used> / <total> GB  (<pct>%)`
   - `CPU:  <pct>%`
   - `Temp: <n>°C` — a **single** value, `max(cpu, gpu)` (the two Apple Silicon
     sensors track within ~1–2°C; one value is enough). `unavailable` if no macmon.
3. **Ember** (see §4.4)
4. **Ledger** (see §4.6)
5. Footer: `Open usage on claude.ai` (link, **Claude only**), `Refresh`.

**Presence rule.** A widget's section (and its separator) is rendered **only when
its tool is present**; otherwise it is omitted entirely — no "unavailable"
placeholder. Presence:
- **Claude** — the desktop app's cookie store exists (`CLAUDE_COOKIES`).
- **Ember** — the CLI is installed (`EMBER_BIN`) **or** the router responds.
- **Ledger** — the CLI is installed (`LEDGER_BIN`) **or** the gateway is up.

A tool that is installed but **not running** still shows (with its offline state +
`Start` action); a tool that isn't installed at all is hidden. **System always
shows** (no external dependency) and anchors the dropdown when Claude is hidden.

All dropdown text is **English**.

### 4.4 Ember widget (`ember_section`)

- Header `Ember — router`.
- **Hidden entirely** when Ember is absent (CLI not at `EMBER_BIN` **and** router
  not responding) — see the presence rule in §4.3.
- If the router does not respond but the CLI is installed: `router offline` +
  `Start (ember serve)`.
- **Current model:** the hot chat model(s) from `GET /status` → `loaded.chat`
  (name, size, idle), green; `none (cold)` if none are loaded.
- **Warm model** submenu: every chat model from `GET /v1/models`
  (excluding `autocomplete`/`embed`), marked ● hot / ○ cold. Clicking warms it.
- **Actions** submenu: `Unload chat`, `Unload all`.
- `Status in terminal` runs `ember status` in Terminal.
- Router base URL: `EMBER_URL` (default `http://127.0.0.1:8000`, override via
  `MLX_ROUTER_HOST`/`MLX_ROUTER_PORT`). CLI binary: `EMBER_BIN`.

### 4.5 Ember actions (`ember_action`)

- `warm <model>` → `POST /v1/chat/completions` with `max_tokens: 1` (loads it).
- `unload <chat|all|name>` → `POST /unload`.
- (`clear` is implemented but omitted from the menu while the production router is
  the bench `mlx_router.py`, which lacks `/clear`. Re-enable under `ember serve`.)

### 4.6 Ledger widget (`ledger_section`)

- Header `Ledger — proxy`.
- **Hidden entirely** when Ledger is absent (CLI not at `LEDGER_BIN` **and** gateway
  not up) — see the presence rule in §4.3.
- **Status line**, from two reads — the gateway's TCP liveness (`LEDGER_HOST:LEDGER_PORT`)
  and whether `env.ANTHROPIC_BASE_URL` in `~/.claude/settings.json` equals `LEDGER_URL`:
  - proxy selected **and** gateway up → `● Proxy ON — Claude → gateway` (green).
  - proxy selected **but** gateway down → `▲ Proxy ON but gateway DOWN — switch to Direct!` (red).
  - proxy not selected → `○ Direct — Claude → Anthropic` (gray).
  - settings unreadable → a single red line; no toggle is offered (never corrupt the file).
- **The switch** (one top-level item, label depends on current state): `Switch to PROXY`
  / `Switch to DIRECT`. Switching to proxy also starts the gateway if it is down.
- **Gateway control** (sibling): `Start gateway` / `Stop gateway`.
- Footer note: `Takes effect in NEW Claude sessions` — the switch writes settings that
  Claude Code reads at session start; it does not retro-fit the running session.
- Config: `LEDGER_URL` (default `http://127.0.0.1:8787`, override via `LEDGER_HOST`/
  `LEDGER_PORT`), `LEDGER_BIN`, `CLAUDE_SETTINGS`.

### 4.7 Ledger actions (`ledger_action`)

- `proxy` → start the gateway if down, then set `env.ANTHROPIC_BASE_URL = LEDGER_URL`.
- `direct` → remove `env.ANTHROPIC_BASE_URL` (and drop an empty `env`).
- `start` / `stop` → launch `ledger gateway` detached / `SIGTERM` whatever listens on
  `LEDGER_PORT` (via `lsof -ti`).
- Settings edits are **atomic** (temp file + `os.replace`) and **key-preserving**: only
  the one env key is touched; `hooks`, `permissions`, etc. are untouched. A missing or
  unparseable settings file is a no-op (read returns `None`).

## 5. Data sources

| Metric | Source |
|---|---|
| RAM, CPU | `psutil` |
| Temperature | `macmon pipe` (Apple Silicon sensors, no sudo) at `/opt/homebrew/bin/macmon` |
| Claude usage | claude.ai usage endpoint, authenticated with the local Claude desktop app session cookie (see §6) |
| Ember | HTTP to the local router |
| Ledger | TCP liveness of the gateway port + the `env.ANTHROPIC_BASE_URL` key in `~/.claude/settings.json` |

## 6. Claude usage acquisition

1. Copy the Claude desktop app cookie store
   (`~/Library/Application Support/Claude/Cookies`, SQLite) and read the
   `sessionKey` + `lastActiveOrg` rows for `claude.ai`.
2. Decrypt with the macOS Keychain key **"Claude Safe Storage"** using the
   Chromium scheme: PBKDF2-SHA1 (salt `saltysalt`, 1003 iters, 16 B) → AES-128-CBC
   (IV = 16 spaces) → strip PKCS7; newer Chromium prepends a 32-byte domain hash.
3. `GET https://claude.ai/api/organizations/<org>/usage` with a **Chrome-impersonated
   TLS fingerprint** (`curl_cffi`, `impersonate="chrome"`). Plain HTTP clients are
   blocked by Cloudflare (JA3/TLS check, 403 "Just a moment").
4. Read `five_hour.utilization` (session), `seven_day.utilization` (weekly),
   `seven_day_opus`, `seven_day_sonnet`, and `resets_at` times.

This is an **undocumented** endpoint and may break; the Claude widget must degrade
gracefully (the rest keeps working) if so.

## 7. Non-blocking refresh

- The render path **must never** make the claude.ai network call inline (a slow
  fetch would freeze RAM/temp too, since it's one process).
- `claude_usage_cached()` returns the cached JSON immediately. If the cache is
  older than `USAGE_TTL` (120 s), it spawns a **detached** background worker
  (`Popen([... , "fetch-usage"])`, `start_new_session=True`) and still returns the
  current (possibly stale) cache.
- A lock file (`/tmp/tally_fetch.lock`) throttles refresh attempts to at most one
  per `USAGE_RETRY` (30 s), even when the fetch keeps failing.
- The background worker writes `/tmp/tally_usage.json` atomically (temp + rename).
- System metrics (psutil/macmon) are cheap and run inline every 5 s.

## 8. Privacy & security (requirements)

- Tally talks only to `claude.ai` (the host the app already uses) and to the
  **local** Ember router. No third-party servers, no telemetry.
- The session key is read into memory, used for one request, and discarded. It is
  **never** written to disk or sent anywhere else.
- Only non-sensitive data is cached: usage **percentages and reset times**
  (`/tmp/tally_usage.json`). No cookies, no session key, no org id.
- The repository must not contain secrets: `.gitignore` blocks `*cookie*`,
  `*session*`, the cache file, and `*_ck`. Source code references cookie **names**,
  never values.
- First run triggers a one-time Keychain prompt for "Claude Safe Storage";
  the user approves **Always Allow** (ACL is bound to `/usr/bin/python3`).

## 9. Configuration

- Refresh cadence: the filename suffix (`tally.5s.py` = 5 s; rename to `.10s.py`, …).
- `USAGE_TTL` (120) — max staleness of the Claude cache before a background refresh.
- `USAGE_RETRY` (30) — min gap between refresh attempts.
- `GREEN_MAX` (50), `YELLOW_MAX` (85) — thresholds for the **dropdown text** colour
  (orange above 50, red above 85). The menu bar bars are monochrome.
- `BATT_OUTLINE`, `BATT_FILL` — battery glyph colours.
- `MENUBAR_BAR_H`, `MENUBAR_PILL_H` — bar image height vs body thickness.
- `EMBER_URL`, `EMBER_BIN` (+ `MLX_ROUTER_HOST`/`MLX_ROUTER_PORT`).
- `LEDGER_URL`, `LEDGER_BIN`, `CLAUDE_SETTINGS` (+ `LEDGER_HOST`/`LEDGER_PORT`).

## 10. Extensibility (adding a widget)

A widget contributes (a) optional content to the menu bar title and/or (b) a
dropdown section. To add one:

1. Gather its data with a **fast, non-blocking** call (cache + background worker if
   it touches the network — follow §7).
2. Print a `---` separator and a `Name | size=13` header, then its rows in English.
3. For clickable actions, add a `__main__` verb guard and emit
   `bash=<realpath(__file__)> param1=<verb> …  terminal=false refresh=true`.
4. Keep the menu bar title within `MENUBAR_H`.

## 11. Acceptance criteria

- A render completes in well under the 5 s refresh interval even when claude.ai is
  slow or unreachable (no inline network wait).
- With Pillow present, the title is a PNG of two identical monochrome battery-style
  S/W bars + RAM + temp icon; without Pillow, a unicode fallback renders and
  nothing crashes.
- The menu bar bars are monochrome and identical in style; the dropdown Claude/RAM
  text turns orange above 50% and red above 85%.
- Ember submenu lists chat models only; warming one makes it show as "Current
  model" on the next refresh; unload clears it.
- Ledger switch is idempotent and round-trips: `proxy` then `direct` returns
  `~/.claude/settings.json` to its prior content with `hooks`/`permissions` intact;
  the status line reflects the live gateway + selected route on the next refresh.
- Presence rule: with a tool absent (Claude app / Ember CLI+router / Ledger
  CLI+gateway), its section and separator are omitted; System still renders and the
  dropdown stays well-formed. An installed-but-stopped tool still shows its `Start`.
- After uninstalling/clearing, no cookie or session material is left on disk.
- The codebase carries no references to the project's former name (rename complete).

## 12. Dependencies & environment

- Python at `/usr/bin/python3` (the shebang; Keychain ACL binds to this binary):
  `psutil`, `cryptography`, `curl_cffi`, `Pillow`.
- `macmon` (Homebrew) for temperature.
- SwiftBar (Homebrew cask) as the host.
- Plugin folder: `~/.config/swiftbar-plugins/`.

## 13. Out of scope / future

- Configurable widget order / enable-disable via a config file.
- More widgets (e.g. network, battery, GitHub notifications).
- `clear` actions once the production router exposes `/clear` (ember serve).
- A non-temp fallback for Intel Macs.
