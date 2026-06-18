#!/usr/bin/python3
"""SwiftBar plugin — unified macOS menu bar: system + Claude Pro plan usage.

Menu bar:  S73% W68% · 🧠52% 🌡️64°
  S = Claude 5-hour session usage, W = Claude weekly (all models)
  🧠 = RAM used %, 🌡️ = CPU temperature

Data sources (all local, nothing leaves the machine):
  - RAM / CPU      : psutil
  - CPU/GPU temp   : `macmon pipe` (Apple Silicon, no sudo)
  - Claude usage   : reads the claude.ai session cookie from the Claude desktop
                     app's local cookie store (decrypted via the macOS Keychain
                     key "Claude Safe Storage"), then calls the same usage
                     endpoint the web app uses. Chrome-impersonated TLS
                     (curl_cffi) is required to pass Cloudflare. The session key
                     never leaves your machine and is never written anywhere.

Deps (for /usr/bin/python3):  pip install --user psutil cryptography curl_cffi
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
USAGE_CACHE = "/tmp/sysclaude_usage.json"
USAGE_TTL = 300  # seconds — Claude usage refresh cadence (system metrics refresh every 5s)
WARN, CRIT = 80, 92  # % thresholds for color


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
    tmp = "/tmp/_sysclaude_ck"
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
        impersonate="chrome", timeout=20,
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


def claude_usage_cached():
    try:
        if os.path.exists(USAGE_CACHE) and time.time() - os.path.getmtime(USAGE_CACHE) < USAGE_TTL:
            with open(USAGE_CACHE) as f:
                return json.load(f)
    except Exception:  # noqa: BLE001
        pass
    try:
        u = fetch_usage()
        if u:
            with open(USAGE_CACHE, "w") as f:
                json.dump(u, f)
        return u
    except Exception:  # noqa: BLE001
        # fall back to a stale cache if present, else None
        try:
            with open(USAGE_CACHE) as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return None


def fmt_reset(iso):
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso).astimezone()
        now = datetime.now(timezone.utc).astimezone()
        if dt.date() == now.date():
            return f"hoje {dt:%H:%M}"
        days = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
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


def main():
    import psutil

    vm = psutil.virtual_memory()
    ram_pct = vm.percent
    ram_used = (vm.total - vm.available) / 1024**3
    ram_total = vm.total / 1024**3
    cpu = psutil.cpu_percent(interval=0.3)
    cpu_t, gpu_t = macmon_temp()
    u = claude_usage_cached()

    temp_str = f"{cpu_t:.0f}°" if cpu_t is not None else "—"
    # ---- menu bar title ----
    if u and (u.get("session") is not None or u.get("weekly") is not None):
        s, w = u.get("session"), u.get("weekly")
        warn = "⚠️ " if (s and s >= WARN) or (w and w >= WARN) else ""
        sw = f"S{s if s is not None else '—'}% W{w if w is not None else '—'}%"
        print(f"{warn}{sw} · 🧠{ram_pct:.0f}% 🌡️{temp_str}")
    else:
        print(f"🧠{ram_pct:.0f}% ⚙️{cpu:.0f}% 🌡️{temp_str}")
    print("---")

    # ---- Claude section ----
    print("Claude — plano Pro | size=13")
    if u:
        s, w = u.get("session"), u.get("weekly")
        sc, wc = color_for(s), color_for(w)
        print(f"Sessão (5h): {s if s is not None else '—'}%   reseta {fmt_reset(u.get('session_reset'))} | font=Menlo"
              + (f" color={sc}" if sc else ""))
        print(f"Semanal (todos): {w if w is not None else '—'}%   reseta {fmt_reset(u.get('weekly_reset'))} | font=Menlo"
              + (f" color={wc}" if wc else ""))
        extra = []
        if u.get("opus") is not None:
            extra.append(f"Opus sem. {u['opus']}%")
        if u.get("sonnet") is not None:
            extra.append(f"Sonnet sem. {u['sonnet']}%")
        if extra:
            print("  " + "   ".join(extra) + " | font=Menlo size=11 color=gray")
    else:
        print("indisponível (app desligado / sessão expirada?) | color=gray")
    print("---")

    # ---- system section ----
    print("Sistema | size=13")
    print(f"RAM: {ram_used:.1f} / {ram_total:.0f} GB ({ram_pct:.0f}%) | font=Menlo")
    print(f"CPU: {cpu:.0f}% | font=Menlo")
    if cpu_t is not None:
        col = "red" if cpu_t >= 90 else ("orange" if cpu_t >= 75 else "")
        print(f"Temp: CPU {cpu_t:.0f}°C   GPU {gpu_t:.0f}°C | font=Menlo" + (f" color={col}" if col else ""))
    else:
        print("Temp: indisponível (macmon?) | font=Menlo")
    print("---")
    print("Abrir Uso no claude.ai | href=https://claude.ai/settings/usage")
    print("Atualizar | refresh=true")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print("⚠️ sysclaude")
        print("---")
        print(f"erro: {e}")
        print("Atualizar | refresh=true")
