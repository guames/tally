#!/usr/bin/python3
"""Tally — a customizable macOS menu bar dashboard (SwiftBar plugin).

A single menu bar item that you grow with widgets. Today it shows:
  - Claude Pro usage : S = 5-hour session, W = weekly (slim coloured bars)
  - System           : RAM, CPU, temperature
  - Ember            : local MLX router — current model + warm/unload controls

Data sources (all local, nothing leaves the machine):
  - RAM / CPU      : psutil
  - CPU/GPU temp   : `macmon pipe` (Apple Silicon, no sudo)
  - Claude usage   : reads the claude.ai session cookie from the Claude desktop
                     app's local cookie store (decrypted via the macOS Keychain
                     key "Claude Safe Storage"), then calls the same usage
                     endpoint the web app uses. Chrome-impersonated TLS
                     (curl_cffi) is required to pass Cloudflare. The session key
                     never leaves your machine and is never written anywhere.
  - Ember          : HTTP to the local router (127.0.0.1:8000).

Deps (for /usr/bin/python3):  pip install --user psutil cryptography curl_cffi Pillow
Plus:  brew install macmon
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

CLAUDE_COOKIES = os.path.expanduser("~/Library/Application Support/Claude/Cookies")
USAGE_CACHE = "/tmp/tally_usage.json"
USAGE_LOCK = "/tmp/tally_fetch.lock"  # throttles background refreshes
USAGE_TTL = 120  # s — how stale the Claude cache may get before a bg refresh
USAGE_RETRY = 30  # s — min gap between background refresh attempts (even on failure)
WARN, CRIT = 80, 92  # % thresholds for color

# Ember — local OpenAI-compatible MLX router (github.com/guames/ember)
EMBER_URL = f"http://{os.environ.get('MLX_ROUTER_HOST', '127.0.0.1')}:{os.environ.get('MLX_ROUTER_PORT', '8000')}"
EMBER_BIN = "/opt/homebrew/bin/ember"

# Ledger — anthropic-compatible proxy that sits between Claude Code and the API
# (github.com/guames/ledger). The switch below flips Claude Code between routing
# through the proxy and talking to Anthropic directly — so if the proxy misbehaves
# you can switch off and keep working. The lever is the `env.ANTHROPIC_BASE_URL`
# key in the global Claude Code settings, which Claude reads on each NEW session.
LEDGER_BIN = "/opt/homebrew/bin/ledger"
LEDGER_HOST = os.environ.get("LEDGER_HOST", "127.0.0.1")
LEDGER_PORT = int(os.environ.get("LEDGER_PORT", "8787"))
LEDGER_URL = f"http://{LEDGER_HOST}:{LEDGER_PORT}"
CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")

# ---- look & feel (menu bar) ----
BAR_FULL, BAR_EMPTY, BAR_NONE = "▓", "░", "·"  # smooth shaded progress bar
CAP_L, CAP_R = "⟮", "⟯"                          # rounded end-caps
ICON_SESSION, ICON_WEEK = "⏳", "📆"             # ⏳ 5h session · 📆 weekly
ICON_RAM = ":memorychip:"                        # SF Symbol RAM stick (SwiftBar inline)
VBLOCKS = "▁▂▃▄▅▆▇█"                             # vertical bar: fills bottom→top


# ============================================================ menu bar bars
# Renders the slim S/W progress bars (neutral gray track, dusty fill, % centred)
# as a PNG that SwiftBar shows in the title via `| image=<base64>`. Falls back
# to unicode bars if Pillow is unavailable.
try:
    import base64
    import io
    from PIL import Image, ImageDraw, ImageFont

    HAVE_PIL = True
except Exception:  # noqa: BLE001
    HAVE_PIL = False

# Menu bar bars mimic the macOS battery glyph: a light outline + a neutral fill,
# monochrome so they sit in with the native menu bar icons (no clashing colour,
# no battery "nub"). S and W look identical, only the fill level differs.
BATT_OUTLINE = (196, 196, 202)
BATT_FILL = [(176, 176, 182), (150, 150, 156)]
WHITE = (255, 255, 255)
ROUND_FONT = "/System/Library/Fonts/SFNSRounded.ttf"

# usage bands (the % thresholds) — used to colour the dropdown text (band_color)
GREEN_MAX, YELLOW_MAX = 50, 85


def _vgrad(w, h, stops):
    g = Image.new("RGB", (1, h))
    n = len(stops)
    for y in range(h):
        seg = (y / max(1, h - 1)) * (n - 1)
        i = min(int(seg), n - 2)
        f = seg - i
        g.putpixel((0, y), tuple(round(stops[i][k] + (stops[i + 1][k] - stops[i][k]) * f) for k in range(3)))
    return g.resize((w, h)).convert("RGBA")


def _rmask(w, h, r):
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
    return m


def _draw_bar(pct, H, W, S, bar_text, pill_h=None):
    """macOS-battery-style glyph: a rounded outline (no nub) + a neutral fill at
    the % level + the % centered inside. Monochrome (S and W look identical). The
    body occupies `pill_h` (≤ H), centered; the % font is sized from the full
    image height so the body can stay slim. Supersampled RGBA."""
    if pill_h is None:
        pill_h = H
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    py = (H - pill_h) // 2
    rad = int(pill_h * 0.34)               # battery-like rounded rectangle (not a pill)
    sw = max(2, int(pill_h * 0.11))        # outline thickness
    # battery body — outline only, no fill, no nub
    d.rounded_rectangle([sw // 2, py + sw // 2, W - 1 - sw // 2, py + pill_h - 1 - sw // 2],
                        radius=rad, outline=BATT_OUTLINE, width=sw)
    # interior (inset from the outline, like the gap inside a battery)
    gap = sw + max(1, S)
    ix, iy = gap, py + gap
    iw, ih = W - 2 * gap, pill_h - 2 * gap
    irad = max(1, int(ih * 0.30))
    p = max(0, min(100, pct if pct is not None else 0)) / 100
    fw = int(iw * p)
    if fw > 1:
        img.paste(_vgrad(fw, ih, BATT_FILL), (ix, iy), _rmask(fw, ih, irad))
    # centered percentage (white, thin dark stroke so it reads over fill or empty)
    if bar_text:
        f = ImageFont.truetype(ROUND_FONT, int(H * 0.56))
        tb = d.textbbox((0, 0), bar_text, font=f)
        d.text((W / 2 - (tb[2] - tb[0]) / 2 - tb[0], H / 2 - (tb[3] - tb[1]) / 2 - tb[1]),
               bar_text, font=f, fill=(245, 245, 247), stroke_width=max(1, S), stroke_fill=(36, 36, 40))
    return img


def render_bar(pct, *, h=34, scale=4, width=180, bar_text=None, label=None, pill_h=None):
    """New-style pill bar; optional `label` letter (S/W) drawn to its left.
    `pill_h` (final px) thins the pill without shrinking the %."""
    S = scale
    H, Wb = h * S, width * S
    bar = _draw_bar(pct, H, Wb, S, bar_text, pill_h * S if pill_h else None)
    if label:
        lab = int(H * 0.62)
        full = Image.new("RGBA", (lab + Wb, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(full)
        f = ImageFont.truetype(ROUND_FONT, int(H * 0.60))
        tb = d.textbbox((0, 0), label, font=f)
        d.text((lab * 0.10 - tb[0], H / 2 - (tb[3] - tb[1]) / 2 - tb[1]),
               label, font=f, fill=(236, 236, 242), stroke_width=max(1, S // 2), stroke_fill=(0, 0, 0))
        full.alpha_composite(bar, (lab, 0))
        bar = full
    return bar.resize((bar.width // S, bar.height // S), Image.LANCZOS)


def _b64(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


MENUBAR_H = 44       # total strip height (px) — SwiftBar caps the title image
MENUBAR_BAR_H = 24   # per-bar image height (drives the % font size)
MENUBAR_PILL_H = 14  # slim pill thickness (≈10px thinner than the image)


def menubar_image(specs):
    """specs: list of dicts (pct + optional badge_text/bar_text/width).
    Combines compact bars into one strip MENUBAR_H px tall, with the (shorter)
    bars vertically centered — so they show at ~half the menu bar height."""
    parts = []
    for sp in specs:
        parts.append(render_bar(
            sp["pct"], h=MENUBAR_BAR_H, width=sp.get("width", 50), pill_h=MENUBAR_PILL_H,
            label=sp.get("label"), bar_text=sp.get("bar_text")))
    gap = 6
    cw = sum(p.width for p in parts) + gap * (len(parts) - 1)
    pad = (MENUBAR_H - MENUBAR_BAR_H) // 2
    strip = Image.new("RGBA", (cw, MENUBAR_H), (0, 0, 0, 0))
    x = 0
    for part in parts:
        strip.alpha_composite(part, (x, pad))
        x += part.width + gap
    return _b64(strip)


# ---------------------------------------------------------------- Claude usage
def _keychain_key():
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    pw = subprocess.run(
        ["security", "find-generic-password", "-ws", "Claude Safe Storage"],
        capture_output=True, text=True,
    ).stdout.strip()
    if not pw:
        raise RuntimeError("no keychain key")
    return PBKDF2HMAC(
        algorithm=hashes.SHA1(), length=16, salt=b"saltysalt",
        iterations=1003, backend=default_backend(),
    ).derive(pw.encode())


def _decrypt(enc, key):
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if enc[:3] not in (b"v10", b"v11"):
        return None
    d = Cipher(algorithms.AES(key), modes.CBC(b" " * 16), backend=default_backend()).decryptor()
    pt = d.update(enc[3:]) + d.finalize()
    pt = pt[: -pt[-1]]  # strip PKCS7 padding
    for cand in (pt, pt[32:]):  # newer Chromium prepends a 32-byte domain hash
        try:
            s = cand.decode()
            if s.isprintable():
                return s
        except Exception:  # noqa: BLE001
            continue
    return pt[32:].decode("utf-8", "ignore")


def _cookies():
    tmp = "/tmp/_tally_ck"
    shutil.copy(CLAUDE_COOKIES, tmp)
    try:
        con = sqlite3.connect(tmp)
        rows = con.execute(
            "select name, encrypted_value from cookies where host_key like '%claude.ai%'"
        ).fetchall()
        con.close()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    key = _keychain_key()
    return {n: _decrypt(e, key) for n, e in rows}


def fetch_usage():
    """Returns dict {session, weekly, opus, sonnet, session_reset, weekly_reset} or None."""
    from curl_cffi import requests as creq

    jar = _cookies()
    org = jar.get("lastActiveOrg")
    if not org or not jar.get("sessionKey"):
        return None
    r = creq.get(
        f"https://claude.ai/api/organizations/{org}/usage",
        cookies={n: v for n, v in jar.items() if v},
        impersonate="chrome", timeout=12,
    )
    if r.status_code != 200:
        return None
    d = r.json()

    def util(key):
        v = d.get(key)
        return round(v["utilization"]) if isinstance(v, dict) and v.get("utilization") is not None else None

    fh, sd = d.get("five_hour") or {}, d.get("seven_day") or {}
    return {
        "session": util("five_hour"),
        "weekly": util("seven_day"),
        "opus": util("seven_day_opus"),
        "sonnet": util("seven_day_sonnet"),
        "session_reset": fh.get("resets_at"),
        "weekly_reset": sd.get("resets_at"),
    }


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def _read_usage():
    try:
        with open(USAGE_CACHE) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def refresh_usage_cache():
    """Fetch from claude.ai and write the cache (run in a background process so
    the network call never blocks the menu render)."""
    u = fetch_usage()
    if u:
        tmp = USAGE_CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(u, f)
        os.replace(tmp, USAGE_CACHE)


def claude_usage_cached():
    """Never blocks on the network: returns the cached value immediately and, if
    it's stale, kicks off a throttled detached background refresh."""
    if time.time() - _mtime(USAGE_CACHE) >= USAGE_TTL and time.time() - _mtime(USAGE_LOCK) >= USAGE_RETRY:
        try:
            open(USAGE_LOCK, "w").close()  # mark the attempt (throttle, even on failure)
            subprocess.Popen(
                [sys.executable, os.path.realpath(__file__), "fetch-usage"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
        except Exception:  # noqa: BLE001
            pass
    return _read_usage()


def fmt_reset(iso):
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso).astimezone()
        now = datetime.now(timezone.utc).astimezone()
        if dt.date() == now.date():
            return f"today {dt:%H:%M}"
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return f"{days[dt.weekday()]} {dt:%H:%M}"
    except Exception:  # noqa: BLE001
        return ""


def color_for(pct):
    if pct is None:
        return ""
    if pct >= CRIT:
        return "red"
    if pct >= WARN:
        return "orange"
    return ""


def band_color(pct):
    """SwiftBar text colour matching the bar bands (green ≤50 / yellow ≤85 / red)."""
    if pct is None:
        return ""
    if pct > YELLOW_MAX:
        return "red"
    if pct > GREEN_MAX:
        return "orange"
    return ""


def bar_centered(pct, width=7):
    """Smooth bar with the percentage centered inside it, e.g. ⟮▓▓86%▓░⟯."""
    label = f"{pct}%" if pct is not None else "—"
    width = max(width, len(label))
    if pct is None:
        cells = [BAR_NONE] * width
    else:
        filled = max(0, min(width, round(pct / 100 * width)))
        cells = [BAR_FULL] * filled + [BAR_EMPTY] * (width - filled)
    start = (width - len(label)) // 2
    cells[start:start + len(label)] = list(label)
    return CAP_L + "".join(cells) + CAP_R


def metric(icon, pct, width=7):
    """icon + bar with centered percentage, e.g. ⏳ ⟮▓▓86%▓░⟯."""
    return f"{icon} {bar_centered(pct, width)}"


def vbar(pct):
    """Single-cell vertical bar filling bottom→top, e.g. ▆."""
    if pct is None:
        return BAR_NONE
    return VBLOCKS[max(0, min(len(VBLOCKS) - 1, round(pct / 100 * (len(VBLOCKS) - 1))))]


# ---------------------------------------------------------------- system
def macmon_temp():
    try:
        r = subprocess.run(
            ["/opt/homebrew/bin/macmon", "pipe", "-s", "1", "-i", "200"],
            capture_output=True, text=True, timeout=5,
        )
        t = json.loads(r.stdout).get("temp", {})
        return t.get("cpu_temp_avg"), t.get("gpu_temp_avg")
    except Exception:  # noqa: BLE001
        return None, None


# ---------------------------------------------------------------- Ember router
def _ember_get(path, timeout=2):
    import urllib.request

    try:
        with urllib.request.urlopen(EMBER_URL + path, timeout=timeout) as r:  # noqa: S310
            return json.load(r)
    except Exception:  # noqa: BLE001
        return None


def _ember_post(path, body, timeout=180):
    import urllib.request

    req = urllib.request.Request(
        EMBER_URL + path, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return json.load(r)


def ember_action(verb, arg):
    """Run an action against the router (used when SwiftBar invokes us with args)."""
    if verb == "warm":
        _ember_post("/v1/chat/completions",
                    {"model": arg, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1})
    elif verb == "unload":
        _ember_post("/unload", {"target": arg or "chat"})
    elif verb == "clear":
        _ember_post("/clear", {"target": arg or "all"})


def ember_section():
    """Prints the Ember dropdown section: current model + warm/unload/clear menu."""
    me = os.path.realpath(__file__)
    st = _ember_get("/status")
    print("---")
    print("Ember — router | size=13")
    if st is None:
        print("router offline | color=gray")
        print(f"Start (ember serve) | bash={EMBER_BIN} param1=serve terminal=true")
        return
    hot = st.get("loaded", {}).get("chat", [])
    if hot:
        for c in hot:
            idle = _dur(c.get("idle_s"))
            print(f"Current model: {c['name']}  ({c['size_gb']:.1f}G · idle {idle}) | color=green font=Menlo")
    else:
        print("Current model: none (cold) | color=gray font=Menlo")

    models = _ember_get("/v1/models")
    hotset = {c["name"] for c in hot}
    if models:
        chat = [m["id"] for m in models.get("data", [])
                if not any(x in m["id"] for x in ("autocomplete", "embed"))]
        print("Warm model | size=12")
        for name in chat:
            mark = "●" if name in hotset else "○"
            print(f"--{mark} {name} | bash={me} param1=ember param2=warm param3={name} terminal=false refresh=true")
    print("Actions | size=12")
    print(f"--Unload chat | bash={me} param1=ember param2=unload param3=chat terminal=false refresh=true")
    print(f"--Unload all | bash={me} param1=ember param2=unload param3=all terminal=false refresh=true")
    print(f"Status in terminal | bash={EMBER_BIN} param1=status terminal=true")


# ---------------------------------------------------------------- Ledger proxy
def _ledger_up():
    """True if something is listening on the gateway port (best-effort liveness)."""
    import socket

    try:
        with socket.create_connection((LEDGER_HOST, LEDGER_PORT), timeout=0.3):
            return True
    except OSError:
        return False


def _read_settings():
    """Parse the global Claude settings, or None if missing/unreadable.
    Never fabricate — a None means 'don't touch', so we can't corrupt the file."""
    try:
        with open(CLAUDE_SETTINGS) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _proxy_active(settings):
    """True if Claude Code is currently configured to route through the gateway."""
    if not isinstance(settings, dict):
        return False
    return (settings.get("env") or {}).get("ANTHROPIC_BASE_URL") == LEDGER_URL


def _set_proxy(on):
    """Flip the env.ANTHROPIC_BASE_URL key in the Claude settings, atomically,
    preserving every other key (hooks, permissions, …). No-op if unreadable."""
    settings = _read_settings()
    if settings is None:
        return
    env = settings.get("env")
    if not isinstance(env, dict):
        env = {}
    if on:
        env["ANTHROPIC_BASE_URL"] = LEDGER_URL
    else:
        env.pop("ANTHROPIC_BASE_URL", None)
    if env:
        settings["env"] = env
    else:
        settings.pop("env", None)  # leave the file clean when nothing is left
    tmp = CLAUDE_SETTINGS + ".tally.tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, CLAUDE_SETTINGS)


def _start_gateway():
    subprocess.Popen(
        [LEDGER_BIN, "gateway", "--host", LEDGER_HOST, "--port", str(LEDGER_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )


def _stop_gateway():
    import signal

    out = subprocess.run(
        ["lsof", "-ti", f"tcp:{LEDGER_PORT}"], capture_output=True, text=True,
    ).stdout.split()
    for pid in out:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (ValueError, ProcessLookupError, PermissionError):
            pass


def ledger_action(verb):
    """Invoked when SwiftBar re-runs us with param1=ledger. Flips the switch."""
    if verb == "proxy":
        if not _ledger_up():
            _start_gateway()
        _set_proxy(True)
    elif verb == "direct":
        _set_proxy(False)
    elif verb == "start":
        _start_gateway()
    elif verb == "stop":
        _stop_gateway()


def ledger_section():
    """Prints the Ledger dropdown: proxy on/off + gateway up/down + toggles."""
    me = os.path.realpath(__file__)
    settings = _read_settings()
    up = _ledger_up()
    active = _proxy_active(settings)
    print("---")
    print("Ledger — proxy | size=13")
    if settings is None:
        print("Claude settings.json unreadable | color=red font=Menlo")
        return
    # status line — green when consistent, RED when the proxy is selected but down
    if active and up:
        print(f"● Proxy ON — Claude → gateway :{LEDGER_PORT} | color=green font=Menlo")
    elif active and not up:
        print("▲ Proxy ON but gateway DOWN — switch to Direct! | color=red font=Menlo")
    else:
        print("○ Direct — Claude → Anthropic | color=gray font=Menlo")
    # the switch (one-click; switching to proxy also starts the gateway if needed)
    if active:
        print(f"Switch to DIRECT (bypass proxy) | bash={me} param1=ledger param2=direct "
              "terminal=false refresh=true")
    else:
        print(f"Switch to PROXY (via gateway) | bash={me} param1=ledger param2=proxy "
              "terminal=false refresh=true")
    # gateway process control (sibling of the switch)
    if up:
        print(f"Stop gateway | bash={me} param1=ledger param2=stop terminal=false refresh=true")
    else:
        print(f"Start gateway | bash={me} param1=ledger param2=start terminal=false refresh=true")
    print("Takes effect in NEW Claude sessions | size=11 color=gray")


def _dur(s):
    if s is None or s < 0:
        return "∞"
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s) // 60}m"
    return f"{int(s) // 3600}h"


def main():
    import psutil

    vm = psutil.virtual_memory()
    ram_pct = vm.percent
    ram_used = (vm.total - vm.available) / 1024**3
    ram_total = vm.total / 1024**3
    ram_str = f"{ICON_RAM} {vbar(ram_pct)} {ram_used:.1f}/{ram_total:.0f}GB"  # usado/total
    cpu = psutil.cpu_percent(interval=0.3)
    cpu_t, gpu_t = macmon_temp()
    u = claude_usage_cached()

    temps = [t for t in (cpu_t, gpu_t) if t is not None]
    temp_one = max(temps) if temps else None
    # leading thin space (U+2009 ≈ 2px) so the number isn't glued to the icon
    temp_str = f" {temp_one:.0f}°" if temp_one is not None else " —"
    THERMO = ":thermometer.medium:"  # flat SF Symbol (replaces the tilted 🌡️ emoji)
    ram_t = f"{ram_used:.1f}/{ram_total:.0f}GB"
    # ---- menu bar title ----
    if u and (u.get("session") is not None or u.get("weekly") is not None):
        s, w = u.get("session"), u.get("weekly")
        if HAVE_PIL:
            img = menubar_image([
                {"pct": s, "label": "S", "bar_text": f"{s}%" if s is not None else "—"},
                {"pct": w, "label": "W", "bar_text": f"{w}%" if w is not None else "—"},
            ])
            print(f"{ram_t} {THERMO}{temp_str} | image={img}")
        else:
            warn = "⚠️ " if (s and s >= WARN) or (w and w >= WARN) else ""
            sw = f"{metric(ICON_SESSION, s)}  {metric(ICON_WEEK, w)}"
            print(f"{warn}{sw} · {ram_str} {THERMO}{temp_str}")
    else:
        print(f"{ram_t} ⚙️{cpu:.0f}% {THERMO}{temp_str}")
    print("---")

    # ---- Claude section ----
    print("Claude — Pro | size=13")
    if u:
        s, w = u.get("session"), u.get("weekly")
        sr, wr = fmt_reset(u.get("session_reset")), fmt_reset(u.get("weekly_reset"))
        sv = f"{s}%" if s is not None else "—"
        wv = f"{w}%" if w is not None else "—"
        print(f"S  Session 5h: {sv}  ·  resets {sr} | font=Menlo"
              + (f" color={band_color(s)}" if band_color(s) else ""))
        print(f"W  Weekly:    {wv}  ·  resets {wr} | font=Menlo"
              + (f" color={band_color(w)}" if band_color(w) else ""))
        extra = []
        if u.get("opus") is not None:
            extra.append(f"Opus 7d {u['opus']}%")
        if u.get("sonnet") is not None:
            extra.append(f"Sonnet 7d {u['sonnet']}%")
        if extra:
            print("  " + "   ".join(extra) + " | font=Menlo size=11 color=gray")
    else:
        print("unavailable (app closed / session expired?) | color=gray")
    print("---")

    # ---- system section ----
    print("System | size=13")
    print(f"RAM:  {ram_used:.1f} / {ram_total:.0f} GB  ({ram_pct:.0f}%) | font=Menlo"
          + (f" color={band_color(ram_pct)}" if band_color(ram_pct) else ""))
    print(f"CPU:  {cpu:.0f}% | font=Menlo")
    if temp_one is not None:
        col = "red" if temp_one >= 90 else ("orange" if temp_one >= 75 else "")
        print(f"Temp: {temp_one:.0f}°C | font=Menlo" + (f" color={col}" if col else ""))
    else:
        print("Temp: unavailable (macmon?) | font=Menlo")

    # ---- Ember section ----
    ember_section()

    # ---- Ledger section ----
    ledger_section()

    print("---")
    print("Open usage on claude.ai | href=https://claude.ai/settings/usage")
    print("Refresh | refresh=true")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "fetch-usage":
        try:
            refresh_usage_cache()
        except Exception:  # noqa: BLE001
            pass
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "ledger":
        try:
            ledger_action(sys.argv[2] if len(sys.argv) > 2 else "")
        except Exception as e:  # noqa: BLE001
            print(e)
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "ember":
        try:
            ember_action(sys.argv[2] if len(sys.argv) > 2 else "",
                         sys.argv[3] if len(sys.argv) > 3 else "")
        except Exception as e:  # noqa: BLE001
            print(e)
        sys.exit(0)
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print("⚠️ tally")
        print("---")
        print(f"error: {e}")
        print("Refresh | refresh=true")
