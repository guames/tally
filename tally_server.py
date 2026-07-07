#!/usr/bin/env python3
"""tally_server.py — Tally as an HTTP dashboard served on the LAN (:8080).

Same data as the SwiftBar plugin; renders as an auto-refreshing HTML page.
Also exposes /data for raw JSON.

Usage:  python3 tally_server.py [port]
LaunchAgent:  com.user.tally-web  (loads automatically at login)
"""

import html as _html
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── Config ──────────────────────────────────────────────────────────────────
PORT      = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
CACHE_TTL = 20   # seconds between background data refreshes

CLAUDE_COOKIES  = os.path.expanduser("~/Library/Application Support/Claude/Cookies")
EMBER_URL       = "http://{}:{}".format(
    os.environ.get("MLX_ROUTER_HOST", "127.0.0.1"),
    os.environ.get("MLX_ROUTER_PORT", "8000"),
)
LEDGER_HOST     = os.environ.get("LEDGER_HOST", "127.0.0.1")
LEDGER_PORT_INT = int(os.environ.get("LEDGER_PORT", "8787"))
LEDGER_URL      = f"http://{LEDGER_HOST}:{LEDGER_PORT_INT}"
CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")
EMBER_BIN       = "/opt/homebrew/bin/ember"
LEDGER_BIN      = "/opt/homebrew/bin/ledger"


# ── Claude usage ─────────────────────────────────────────────────────────────
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
    pt = pt[: -pt[-1]]
    for cand in (pt, pt[32:]):
        try:
            s = cand.decode()
            if s.isprintable():
                return s
        except Exception:
            continue
    return pt[32:].decode("utf-8", "ignore")


def _cookies():
    tmp = "/tmp/_tally_web_ck"
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


def _fetch_usage():
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

    def util(k):
        v = d.get(k)
        return round(v["utilization"]) if isinstance(v, dict) and v.get("utilization") is not None else None

    fh, sd = d.get("five_hour") or {}, d.get("seven_day") or {}
    result = {
        "session":       util("five_hour"),
        "weekly":        util("seven_day"),
        "opus":          util("seven_day_opus"),
        "sonnet":        util("seven_day_sonnet"),
        "session_reset": fh.get("resets_at"),
        "weekly_reset":  sd.get("resets_at"),
    }
    eu = d.get("extra_usage") or {}
    if eu.get("utilization") is not None:
        div = 10 ** eu.get("decimal_places", 2)
        result["monthly_pct"]      = round(eu["utilization"])
        result["monthly_used"]     = (eu.get("used_credits") or 0) / div
        result["monthly_limit"]    = (eu.get("monthly_limit") or 0) / div
        result["monthly_currency"] = eu.get("currency", "USD")
    return result


USAGE_CACHE = "/tmp/tally_usage.json"
USAGE_TTL   = 300  # s — the plugin refreshes it at 120s; we only fetch when it goes stale
USAGE_RETRY = 60   # s — min gap between our own fetch attempts (even on failure)

_usage_last_try = 0.0


def _usage_cached():
    """Reuse the cache that tally.15s.py maintains; fetch ourselves only when it
    goes stale (e.g. SwiftBar stopped). Stale-on-failure: a broken fetch serves
    the old value rather than nothing, and retries are throttled so a dead
    session key doesn't hammer claude.ai every refresh loop."""
    global _usage_last_try
    cached = None
    try:
        with open(USAGE_CACHE) as f:
            cached = json.load(f)
        if time.time() - os.path.getmtime(USAGE_CACHE) < USAGE_TTL:
            return cached
    except Exception:
        pass
    if not os.path.exists(CLAUDE_COOKIES):
        return cached
    if time.time() - _usage_last_try < USAGE_RETRY:
        return cached
    _usage_last_try = time.time()
    try:
        u = _fetch_usage()
    except Exception:
        u = None
    if not u:
        return cached
    try:
        tmp = USAGE_CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(u, f)
        os.replace(tmp, USAGE_CACHE)
    except Exception:
        pass
    return u


def _fmt_reset(iso):
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso).astimezone()
        now = datetime.now(timezone.utc).astimezone()
        if dt.date() == now.date():
            return f"today {dt:%H:%M}"
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return f"{days[dt.weekday()]} {dt:%H:%M}"
    except Exception:
        return ""


# ── System ────────────────────────────────────────────────────────────────────
def _macmon_temp():
    try:
        r = subprocess.run(
            ["/opt/homebrew/bin/macmon", "pipe", "-s", "1", "-i", "200"],
            capture_output=True, text=True, timeout=5,
        )
        t = json.loads(r.stdout).get("temp", {})
        return t.get("cpu_temp_avg"), t.get("gpu_temp_avg")
    except Exception:
        return None, None


# ── Ember ─────────────────────────────────────────────────────────────────────
def _ember_get(path, timeout=2):
    import urllib.request
    try:
        with urllib.request.urlopen(EMBER_URL + path, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def _dur(s):
    if s is None or s < 0:
        return "∞"
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s) // 60}m"
    return f"{int(s) // 3600}h"


# ── Ledger ────────────────────────────────────────────────────────────────────
def _ledger_up():
    try:
        with socket.create_connection((LEDGER_HOST, LEDGER_PORT_INT), timeout=0.3):
            return True
    except OSError:
        return False


def _ledger_econ():
    import urllib.request
    try:
        with urllib.request.urlopen(LEDGER_URL + "/__ledger/econ", timeout=0.4) as r:
            sig = json.load(r)
    except Exception:
        return None
    return sig if isinstance(sig, dict) and sig.get("tail_tax_est") is not None else None


def _proxy_active():
    try:
        with open(CLAUDE_SETTINGS) as f:
            s = json.load(f)
        return (s.get("env") or {}).get("ANTHROPIC_BASE_URL") == LEDGER_URL
    except Exception:
        return False


# ── Data snapshot ─────────────────────────────────────────────────────────────
def collect():
    import psutil

    vm  = psutil.virtual_memory()
    cpu = psutil.cpu_percent(interval=0.3)
    cpu_t, gpu_t = _macmon_temp()

    ember_st = _ember_get("/status")

    up     = _ledger_up()
    active = _proxy_active()
    econ   = _ledger_econ() if (up and active) else None

    return {
        "ts":          datetime.now().strftime("%H:%M:%S"),
        "ram_pct":     vm.percent,
        "ram_used_g":  (vm.total - vm.available) / 1024**3,
        "ram_total_g": vm.total / 1024**3,
        "cpu_pct":     cpu,
        "cpu_temp":    cpu_t,
        "gpu_temp":    gpu_t,
        "usage":       _usage_cached(),
        "ember_st":    ember_st,
        "ledger_up":   up,
        "ledger_active": active,
        "ledger_econ": econ,
    }


# ── Background refresh ────────────────────────────────────────────────────────
_state = {"data": {}}
_lock  = threading.Lock()


def _refresh_loop():
    while True:
        try:
            d = collect()
            with _lock:
                _state["data"] = d
        except Exception:
            pass
        time.sleep(CACHE_TTL)


def _get_data():
    with _lock:
        return dict(_state["data"])


# ── HTML renderer ─────────────────────────────────────────────────────────────
def _bar_color(pct):
    if pct is None: return "bg"
    if pct > 85:    return "red"
    if pct > 50:    return "orange"
    return "green"


def _val_color(pct):
    if pct is None: return ""
    if pct > 85:    return " red"
    if pct > 50:    return " orange"
    return " green"


def _bar_row(label, pct, value_str, meta=""):
    bc = _bar_color(pct)
    vc = _val_color(pct)
    w  = max(0, min(100, pct or 0))
    meta_html = f'<span class="meta">{_html.escape(meta)}</span>' if meta else ""
    return (
        f'<div class="row">'
        f'<span class="lbl">{_html.escape(label)}</span>'
        f'<div class="track"><div class="fill {bc}" style="width:{w}%"></div></div>'
        f'<span class="val{vc}">{_html.escape(value_str)}</span>'
        f'{meta_html}'
        f'</div>\n'
    )


def render_html(d):
    # ── Claude ──
    u = d.get("usage") or {}
    claude_body = ""
    if u:
        s, w, m = u.get("session"), u.get("weekly"), u.get("monthly_pct")
        if s is not None:
            claude_body += _bar_row("Session", s, f"{s}%", _fmt_reset(u.get("session_reset")))
        if w is not None:
            claude_body += _bar_row("Weekly", w, f"{w}%", _fmt_reset(u.get("weekly_reset")))
        if m is not None:
            used = u.get("monthly_used", 0)
            lim  = u.get("monthly_limit", 0)
            cur  = u.get("monthly_currency", "USD")
            claude_body += _bar_row("Monthly", m, f"{m}%", f"${used:.0f}/${lim:.0f} {cur}")
        extras = []
        if u.get("opus") is not None:
            extras.append(f"Opus 7d {u['opus']}%")
        if u.get("sonnet") is not None:
            extras.append(f"Sonnet 7d {u['sonnet']}%")
        if extras:
            claude_body += f'<div class="sub">{"  ·  ".join(extras)}</div>\n'
    elif os.path.exists(CLAUDE_COOKIES):
        claude_body = '<div class="sub muted">fetching…</div>\n'

    # ── System ──
    rp  = d.get("ram_pct")
    ru  = d.get("ram_used_g", 0)
    rt  = d.get("ram_total_g", 1)
    cpu = d.get("cpu_pct")
    temp = d.get("cpu_temp")
    sys_body = _bar_row("RAM", rp, f"{ru:.1f}/{rt:.0f}G")
    sys_body += _bar_row("CPU", cpu, f"{cpu:.0f}%" if cpu is not None else "—")
    if temp is not None:
        tc = "red" if temp >= 90 else ("orange" if temp >= 75 else "green")
        sys_body += (
            f'<div class="row">'
            f'<span class="lbl">Temp</span>'
            f'<span class="val {tc}">{temp:.0f}°C</span>'
            f'</div>\n'
        )

    # ── Ember ──
    ember_body = ""
    st = d.get("ember_st")
    if st or os.path.exists(EMBER_BIN):
        if st is None:
            ember_body = '<div class="sub muted">router offline</div>\n'
        else:
            hot = st.get("loaded", {}).get("chat", [])
            if hot:
                c    = hot[0]
                idle = _dur(c.get("idle_s"))
                ember_body = (
                    f'<div class="sline">'
                    f'<span class="green">●&nbsp;</span>'
                    f'<span class="model">{_html.escape(c["name"])}</span>'
                    f'<span class="muted">&ensp;{c["size_gb"]:.1f}G · idle {idle}</span>'
                    f'</div>\n'
                )
                for c2 in hot[1:]:
                    ember_body += f'<div class="sub muted">+ {_html.escape(c2["name"])} ({c2["size_gb"]:.1f}G)</div>\n'
            else:
                ember_body = '<div class="sub muted">cold (no model loaded)</div>\n'

    # ── Ledger ──
    ledger_body = ""
    up     = d.get("ledger_up")
    active = d.get("ledger_active")
    econ   = d.get("ledger_econ")
    if up or os.path.exists(LEDGER_BIN):
        if active and up:
            ledger_body = (
                f'<div class="sline"><span class="green">● Proxy ON</span>'
                f'<span class="muted">&ensp;gateway :{LEDGER_PORT_INT}</span></div>\n'
            )
            if econ and (econ.get("tail_tax_est") or 0) > 0:
                tax   = econ["tail_tax_est"]
                reset = bool(econ.get("suggest_reset"))
                col   = "orange" if reset else "muted"
                hint  = "&ensp;← open new session" if reset else ""
                ledger_body += f'<div class="sub {col}">💸 Tail ~${tax:.2f}{hint}</div>\n'
        elif active and not up:
            ledger_body = '<div class="sline"><span class="red">▲ Proxy ON — gateway DOWN</span></div>\n'
        elif up:
            ledger_body = f'<div class="sline"><span class="muted">○ Direct (gateway up :{LEDGER_PORT_INT})</span></div>\n'
        else:
            ledger_body = '<div class="sub muted">Direct — no gateway</div>\n'

    # ── Assemble sections ──
    secs = []
    if claude_body:
        secs.append(f'<div class="card"><div class="ctitle">Claude</div>{claude_body}</div>')
    secs.append(f'<div class="card"><div class="ctitle">System</div>{sys_body}</div>')
    if ember_body:
        secs.append(f'<div class="card"><div class="ctitle">Ember</div>{ember_body}</div>')
    if ledger_body:
        secs.append(f'<div class="card"><div class="ctitle">Ledger</div>{ledger_body}</div>')

    ts = _html.escape(d.get("ts", "—"))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=800,initial-scale=1">
<meta http-equiv="refresh" content="30">
<meta name="color-scheme" content="dark">
<title>Tally</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1117;color:#c9d1d9;font-family:'SF Mono','Menlo','Courier New',monospace;font-size:14px;padding:14px 16px;min-height:100vh}}
h1{{text-align:center;font-size:11px;letter-spacing:3px;text-transform:uppercase;color:#484f58;margin-bottom:14px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 14px;margin-bottom:10px}}
.ctitle{{font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:#8b949e;margin-bottom:10px}}
.row{{display:flex;align-items:center;gap:8px;margin-bottom:7px;flex-wrap:nowrap}}
.lbl{{font-size:12px;color:#8b949e;width:60px;flex-shrink:0}}
.track{{flex:1;background:#21262d;border-radius:3px;height:10px;overflow:hidden;min-width:40px}}
.fill{{height:100%;border-radius:3px;transition:width .4s ease}}
.green{{color:#3fb950}} .orange{{color:#d29922}} .red{{color:#f85149}} .muted{{color:#6e7681}}
.fill.green{{background:#3fb950}} .fill.orange{{background:#d29922}} .fill.red{{background:#f85149}} .fill.bg{{background:#484f58}}
.val{{font-size:13px;min-width:54px;text-align:right;font-weight:500;flex-shrink:0}}
.meta{{font-size:11px;color:#6e7681;white-space:nowrap}}
.sub{{font-size:11px;color:#6e7681;padding:2px 0 2px 68px}}
.sline{{display:flex;align-items:baseline;padding:2px 0}}
.model{{font-weight:500;color:#c9d1d9}}
.ts{{text-align:right;font-size:10px;color:#30363d;margin-top:10px}}
</style>
</head>
<body>
<h1>TALLY</h1>
{"".join(secs)}
<p class="ts">updated {ts}</p>
</body>
</html>"""


# ── HTTP handler ──────────────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/data":
            body = json.dumps(_get_data(), default=str).encode()
            self._respond(200, "application/json", body)
        elif self.path in ("/", "/index.html"):
            body = render_html(_get_data()).encode()
            self._respond(200, "text/html; charset=utf-8", body)
        else:
            self.send_error(404)

    def _respond(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"[tally-web] collecting initial data…", flush=True)
    try:
        with _lock:
            _state["data"] = collect()
        print(f"[tally-web] ready", flush=True)
    except Exception as e:
        print(f"[tally-web] initial collect warning: {e}", file=sys.stderr, flush=True)

    threading.Thread(target=_refresh_loop, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), _Handler)
    print(f"[tally-web] serving http://0.0.0.0:{PORT}/  (refresh every {CACHE_TTL}s)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
