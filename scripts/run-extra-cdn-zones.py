#!/usr/bin/env python3
"""Fleet: add telegrambot.website + teleworker.fun CDN domains + apply."""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLINK = Path(r"C:\Program Files\PuTTY\plink.exe")
PSCP = Path(r"C:\Program Files\PuTTY\pscp.exe")
DO_SH = ROOT / "artifacts" / "extra-cdn-do.sh"
ZONES = ["telegrambot.website", "teleworker.fun"]
OUT = ROOT / "artifacts" / "extra-cdn-zones-results.json"


def load_dotenv() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def parse_inv() -> list[dict]:
    text = (ROOT / "inventory.yml").read_text(encoding="utf-8", errors="replace")
    servers = []
    for p in re.split(r"(?m)^(?=  - id:)", text)[1:]:
        m_id = re.search(r"(?m)^\s+- id:\s*(\S+)", p)
        m_host = re.search(r"(?m)^\s+host:\s*(\S+)", p)
        if not m_id or not m_host:
            continue
        sid = m_id.group(1)
        if not sid.startswith("vpn-vp"):
            continue
        pe = re.search(r"(?m)^\s+password_env:\s*(\S+)", p)
        sub = re.search(r"(?m)^\s+subdomain:\s*(\S+)", p)
        sport = re.search(r"(?m)^\s+ssh_port:\s*(\d+)", p)
        servers.append(
            {
                "id": sid,
                "host": m_host.group(1),
                "password_env": pe.group(1) if pe else None,
                "subdomain": sub.group(1) if sub else sid.replace("vpn-", ""),
                "ssh_port": int(sport.group(1)) if sport else 22,
            }
        )
    return servers


def run(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        return 124, ((e.stdout or "") + (e.stderr or "") + "\nTIMEOUT")


def cf_ensure(token: str, sub: str, ip: str) -> list[str]:
    logs = []

    def api(method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            f"https://api.cloudflare.com/client/v4{path}",
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=40) as resp:
            return json.loads(resp.read().decode())

    for z in ZONES:
        zid = api("GET", f"/zones?name={urllib.parse.quote(z)}")["result"][0]["id"]
        name = f"{sub}.{z}"
        q = urllib.parse.urlencode({"type": "A", "name": name})
        recs = api("GET", f"/zones/{zid}/dns_records?{q}")["result"]
        body = {"type": "A", "name": name, "content": ip, "proxied": True, "ttl": 1}
        if not recs:
            api("POST", f"/zones/{zid}/dns_records", body)
            logs.append(f"CREATE {name}")
        elif recs[0].get("content") != ip or not recs[0].get("proxied"):
            api("PUT", f"/zones/{zid}/dns_records/{recs[0]['id']}", body)
            logs.append(f"UPDATE {name}")
        else:
            logs.append(f"OK {name}")
    return logs


def apply_direct(server: dict, env: dict, names: list[str]) -> dict:
    host, port = server["host"], str(server["ssh_port"])
    pw = env.get(server.get("password_env") or "", "")
    if not pw:
        return {"ok": False, "error": "no password"}
    # upload do.sh + names.json
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8", newline="\n") as f:
        json.dump(names, f)
        names_path = f.name
    try:
        rc, out = run([str(PSCP), "-P", port, "-pw", pw, "-batch", str(DO_SH), names_path, f"root@{host}:/tmp/"], 90)
        if rc != 0:
            return {"ok": False, "error": f"pscp {out[-400:]}"}
        # rename names file on remote
        rc, out = run(
            [
                str(PLINK),
                "-ssh",
                f"root@{host}",
                "-P",
                port,
                "-pw",
                pw,
                "-batch",
                f"mv -f /tmp/{Path(names_path).name} /tmp/extra-cdn-names.json; bash /tmp/extra-cdn-do.sh",
            ],
            700,
        )
        return {"ok": "ALL_OK" in out, "error": None if "ALL_OK" in out else out[-1500:], "log": out[-600:]}
    finally:
        try:
            os.unlink(names_path)
        except OSError:
            pass


def apply_socks(tg: dict, server: dict, env: dict, names: list[str], socks: int) -> dict:
    host, port = server["host"], server["ssh_port"]
    pw = env.get(server.get("password_env") or "", "")
    pw_b64 = base64.b64encode(pw.encode()).decode()
    sh_b64 = base64.b64encode(DO_SH.read_bytes()).decode()
    names_b64 = base64.b64encode(json.dumps(names).encode()).decode()
    body = f"""set -euo pipefail
echo {pw_b64} | base64 -d > /tmp/x.pass; chmod 600 /tmp/x.pass; export SSHPASS=$(cat /tmp/x.pass)
PROXY='nc -X 5 -x 127.0.0.1:{socks} %h %p'
echo {sh_b64} | base64 -d > /tmp/extra-cdn-do.sh
echo {names_b64} | base64 -d > /tmp/extra-cdn-names.json
sshpass -e scp -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/x.known -o ConnectTimeout=25 -o ProxyCommand="$PROXY" -P {port} /tmp/extra-cdn-do.sh /tmp/extra-cdn-names.json root@{host}:/tmp/
sshpass -e ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/x.known -o ConnectTimeout=25 -o ProxyCommand="$PROXY" -p {port} root@{host} 'bash /tmp/extra-cdn-do.sh'
echo JUMP_DONE
"""
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh", encoding="utf-8", newline="\n") as f:
        f.write(body)
        path = f.name
    try:
        rc, out = run(
            [str(PLINK), "-ssh", "-batch", "-pw", tg["ssh_password"], "-m", path, f"{tg['ssh_user']}@{tg['ssh_host']}"],
            800,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return {"ok": "ALL_OK" in out, "error": None if "ALL_OK" in out else out[-1500:], "via": f"socks{socks}"}


def find_socks(tg: dict, host: str, port: int, pw: str) -> int | None:
    pw_b64 = base64.b64encode(pw.encode()).decode()
    body = f"""set +e
echo {pw_b64} | base64 -d > /tmp/x.pass; chmod 600 /tmp/x.pass; export SSHPASS=$(cat /tmp/x.pass)
for p in 1084 1085 1080 1081 1082 1083; do
  out=$(timeout 12 sshpass -e ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/x.known -o ConnectTimeout=8 -o ProxyCommand="nc -X 5 -x 127.0.0.1:$p %h %p" -p {port} root@{host} 'echo SOCKS_OK' 2>/dev/null) || true
  if echo "$out" | grep -q SOCKS_OK; then echo FOUND $p; exit 0; fi
done
echo NONE; exit 1
"""
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh", encoding="utf-8", newline="\n") as f:
        f.write(body)
        path = f.name
    try:
        rc, out = run(
            [str(PLINK), "-ssh", "-batch", "-pw", tg["ssh_password"], "-m", path, f"{tg['ssh_user']}@{tg['ssh_host']}"],
            160,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    m = re.search(r"FOUND (\d+)", out)
    return int(m.group(1)) if m else None


def patch_inventory(servers: list[dict]) -> None:
    path = ROOT / "inventory.yml"
    text = path.read_text(encoding="utf-8")
    for s in servers:
        sid, sub = s["id"], s["subdomain"]
        idx = text.find(f"id: {sid}")
        if idx < 0:
            continue
        end = text.find("\n  - id:", idx + 1)
        if end < 0:
            end = len(text)
        chunk = text[idx:end]
        for z in ZONES:
            name = f"{sub}.{z}"
            if name in chunk:
                continue
            m = re.search(r"(?ms)^(    domains:\n(?:      - .+\n)+)", chunk)
            if m:
                chunk = chunk.replace(m.group(1), m.group(1).rstrip("\n") + f"\n      - {name}\n", 1)
        if re.search(r"(?m)^    status_note:", chunk):
            chunk = re.sub(
                r"(?m)^    status_note:.*$",
                'status_note: "extra CDN: telegrambot.website + teleworker.fun"',
                chunk,
                count=1,
            )
        if re.search(r"(?m)^    status_updated_at:", chunk):
            chunk = re.sub(
                r"(?m)^    status_updated_at:.*$",
                'status_updated_at: "2026-09-28 19:10"',
                chunk,
                count=1,
            )
        text = text[:idx] + chunk + text[end:]
    path.write_text(text, encoding="utf-8")


def main() -> int:
    DO_SH.write_bytes(DO_SH.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
    env = load_dotenv()
    tg = json.loads((ROOT / "telegram.local.json").read_text(encoding="utf-8"))
    token = env.get("CLOUDFLARE_API_TOKEN", "").strip()
    servers = parse_inv()
    only = set(sys.argv[1:])
    if only:
        servers = [s for s in servers if s["id"] in only or s["subdomain"] in only]
    # skip vp1 if already done unless explicitly requested
    print(f"Targets: {len(servers)}", flush=True)
    results = []
    for i, s in enumerate(servers, 1):
        names = [f"{s['subdomain']}.{z}" for z in ZONES]
        print(f"[{i}/{len(servers)}] {s['id']} {s['host']}", flush=True)
        try:
            logs = cf_ensure(token, s["subdomain"], s["host"])
            print("  CF " + "; ".join(logs), flush=True)
        except Exception as e:
            print(f"  CF FAIL {e}", flush=True)
            results.append({"id": s["id"], "ok": False, "error": f"cf {e}"})
            continue
        if s["id"] in ("vpn-vp2", "vpn-vp8"):
            socks = find_socks(tg, s["host"], s["ssh_port"], env.get(s["password_env"], ""))
            r = apply_socks(tg, s, env, names, socks) if socks else apply_direct(s, env, names)
        else:
            r = apply_direct(s, env, names)
            if not r.get("ok"):
                socks = find_socks(tg, s["host"], s["ssh_port"], env.get(s["password_env"], ""))
                if socks:
                    print(f"  retry socks{socks}", flush=True)
                    r = apply_socks(tg, s, env, names, socks)
        print(f"  {'OK' if r.get('ok') else 'FAIL'}", flush=True)
        if not r.get("ok") and r.get("error"):
            print("   " + re.sub(r"\s+", " ", r["error"])[:250], flush=True)
        results.append({"id": s["id"], "domains": names, **{k: v for k, v in r.items() if k != "log"}})
        # progress notify every 5
        if i % 5 == 0:
            try:
                msg = ROOT / "tg-msg.txt"
                msg.write_text(f"Extra CDN zones: {i}/{len(servers)} обработано, ok={sum(1 for x in results if x.get('ok'))}\n", encoding="utf-8")
                subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(ROOT / "telegram-notify.ps1"),
                        "-MessageFile",
                        str(msg),
                    ],
                    capture_output=True,
                    timeout=60,
                )
            except Exception:
                pass
        time.sleep(0.2)

    patch_inventory(servers)
    OUT.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    ok = sum(1 for x in results if x.get("ok"))
    print(f"Done: {ok}/{len(results)} -> {OUT}", flush=True)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
