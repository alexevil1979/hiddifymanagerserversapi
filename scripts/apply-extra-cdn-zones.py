#!/usr/bin/env python3
"""Apply extra CDN domains on fleet using uploaded remote script (pscp+plink)."""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLINK = Path(r"C:\Program Files\PuTTY\plink.exe")
PSCP = Path(r"C:\Program Files\PuTTY\pscp.exe")
REMOTE_SH = ROOT / "artifacts" / "extra-cdn-remote.sh"


def load_dotenv2() -> dict[str, str]:
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
    parts = re.split(r"(?m)^(?=  - id:)", text)
    servers = []
    for p in parts[1:]:
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


def run_cmd(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or "") + (e.stderr or "") + "\nTIMEOUT"


def apply_direct(server: dict, env: dict, names: list[str]) -> dict:
    host = server["host"]
    port = str(server["ssh_port"])
    pw = env.get(server.get("password_env") or "", "")
    if not pw:
        return {"ok": False, "error": "no password"}
    # kill leftover apply
    run_cmd([str(PLINK), "-ssh", f"root@{host}", "-P", port, "-pw", pw, "-batch",
             "pkill -f cli_progress 2>/dev/null; pkill -f apply_configs 2>/dev/null; true"], 30)
    rc, out = run_cmd([str(PSCP), "-P", port, "-pw", pw, "-batch", str(REMOTE_SH), f"root@{host}:/tmp/extra-cdn-remote.sh"], 60)
    if rc != 0:
        return {"ok": False, "error": f"pscp: {out[-300:]}"}
    names_b64 = base64.b64encode(json.dumps(names).encode()).decode()
    remote = f"echo {names_b64} | base64 -d > /tmp/names.json; bash /tmp/extra-cdn-remote.sh \"$(cat /tmp/names.json)\""
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh", encoding="utf-8", newline="\n") as f:
        f.write(remote + "\n")
        path = f.name
    try:
        rc, out = run_cmd([str(PLINK), "-ssh", f"root@{host}", "-P", port, "-pw", pw, "-batch", "-m", path], 900)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return {"ok": "ALL_OK" in out, "error": None if "ALL_OK" in out else out[-1200:], "log": out[-500:]}


def apply_socks(tg: dict, server: dict, env: dict, names: list[str], socks: int) -> dict:
    host = server["host"]
    port = server["ssh_port"]
    pw = env.get(server.get("password_env") or "", "")
    pw_b64 = base64.b64encode(pw.encode()).decode()
    sh_b64 = base64.b64encode(REMOTE_SH.read_bytes()).decode()
    names_b64 = base64.b64encode(json.dumps(names).encode()).decode()
    body = f"""set -euo pipefail
echo {pw_b64} | base64 -d > /tmp/extra-cdn.pass
chmod 600 /tmp/extra-cdn.pass
export SSHPASS=$(cat /tmp/extra-cdn.pass)
PROXY='nc -X 5 -x 127.0.0.1:{socks} %h %p'
echo {sh_b64} | base64 -d > /tmp/extra-cdn-remote.sh
echo {names_b64} | base64 -d > /tmp/names.json
sshpass -e scp -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/extra-cdn.known -o ConnectTimeout=25 -o ProxyCommand="$PROXY" -P {port} /tmp/extra-cdn-remote.sh /tmp/names.json root@{host}:/tmp/
sshpass -e ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/extra-cdn.known -o ConnectTimeout=25 -o ProxyCommand="$PROXY" -p {port} root@{host} 'pkill -f cli_progress 2>/dev/null; pkill -f apply_configs 2>/dev/null; true; bash /tmp/extra-cdn-remote.sh "$(cat /tmp/names.json)"'
echo JUMP_DONE
"""
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh", encoding="utf-8", newline="\n") as f:
        f.write(body)
        path = f.name
    try:
        rc, out = run_cmd(
            [str(PLINK), "-ssh", "-batch", "-pw", tg["ssh_password"], "-m", path, f"{tg['ssh_user']}@{tg['ssh_host']}"],
            1000,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    ok = "ALL_OK" in out
    return {"ok": ok, "error": None if ok else out[-1200:], "via": f"socks{socks}"}


def find_socks_port(tg: dict, host: str, port: int, pw: str) -> int | None:
    pw_b64 = base64.b64encode(pw.encode()).decode()
    body = f"""set +e
echo {pw_b64} | base64 -d > /tmp/x.pass; chmod 600 /tmp/x.pass; export SSHPASS=$(cat /tmp/x.pass)
for p in 1084 1085 1080 1081 1082 1083; do
  out=$(timeout 15 sshpass -e ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/x.known -o ConnectTimeout=10 -o ProxyCommand="nc -X 5 -x 127.0.0.1:$p %h %p" -p {port} root@{host} 'echo SOCKS_OK' 2>/dev/null) || true
  if echo "$out" | grep -q SOCKS_OK; then echo FOUND $p; exit 0; fi
done
echo NONE; exit 1
"""
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh", encoding="utf-8", newline="\n") as f:
        f.write(body)
        path = f.name
    try:
        rc, out = run_cmd(
            [str(PLINK), "-ssh", "-batch", "-pw", tg["ssh_password"], "-m", path, f"{tg['ssh_user']}@{tg['ssh_host']}"],
            180,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    m = re.search(r"FOUND (\d+)", out)
    return int(m.group(1)) if m else None


def patch_inv(servers: list[dict]) -> None:
    path = ROOT / "inventory.yml"
    text = path.read_text(encoding="utf-8")
    zones = ["telegrambot.website", "teleworker.fun"]
    for s in servers:
        sid, sub = s["id"], s["subdomain"]
        idx = text.find(f"id: {sid}")
        if idx < 0:
            continue
        end = text.find("\n  - id:", idx + 1)
        if end < 0:
            end = len(text)
        chunk = text[idx:end]
        for z in zones:
            name = f"{sub}.{z}"
            if name in chunk:
                continue
            m = re.search(r"(?ms)^(    domains:\n(?:      - .+\n)+)", chunk)
            if m:
                block = m.group(1)
                chunk = chunk.replace(block, block.rstrip("\n") + f"\n      - {name}\n", 1)
        chunk = re.sub(
            r"(?m)^    status_note:.*$",
            'status_note: "extra CDN: telegrambot.website + teleworker.fun"',
            chunk,
            count=1,
        )
        chunk = re.sub(
            r"(?m)^    status_updated_at:.*$",
            'status_updated_at: "2026-09-28 19:00"',
            chunk,
            count=1,
        )
        text = text[:idx] + chunk + text[end:]
    path.write_text(text, encoding="utf-8")


def main() -> int:
    # LF remote script
    REMOTE_SH.write_bytes(REMOTE_SH.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
    env = load_dotenv2()
    tg = json.loads((ROOT / "telegram.local.json").read_text(encoding="utf-8"))
    token = env.get("CLOUDFLARE_API_TOKEN", "").strip()
    servers = parse_inv()
    only = set(sys.argv[1:])
    if only:
        servers = [s for s in servers if s["id"] in only or s["subdomain"] in only]
    print(f"Apply panel+configs on {len(servers)} servers", flush=True)
    results = []
    for i, s in enumerate(servers, 1):
        names = [f"{s['subdomain']}.{z}" for z in ("telegrambot.website", "teleworker.fun")]
        print(f"[{i}/{len(servers)}] {s['id']} {s['host']}", flush=True)
        # CF already done; skip unless missing
        try:
            # lightweight ensure
            import urllib.parse, urllib.request

            def cf(method, path, body=None):
                data = None if body is None else json.dumps(body).encode()
                req = urllib.request.Request(
                    f"https://api.cloudflare.com/client/v4{path}",
                    data=data,
                    method=method,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=40) as resp:
                    return json.loads(resp.read().decode())

            for z in ("telegrambot.website", "teleworker.fun"):
                zid = cf("GET", f"/zones?name={urllib.parse.quote(z)}")["result"][0]["id"]
                name = f"{s['subdomain']}.{z}"
                q = urllib.parse.urlencode({"type": "A", "name": name})
                recs = cf("GET", f"/zones/{zid}/dns_records?{q}")["result"]
                body = {"type": "A", "name": name, "content": s["host"], "proxied": True, "ttl": 1}
                if not recs:
                    cf("POST", f"/zones/{zid}/dns_records", body)
                    print(f"  CF CREATE {name}", flush=True)
                elif recs[0].get("content") != s["host"] or not recs[0].get("proxied"):
                    cf("PUT", f"/zones/{zid}/dns_records/{recs[0]['id']}", body)
                    print(f"  CF UPDATE {name}", flush=True)
        except Exception as e:
            print(f"  CF warn: {e}", flush=True)

        if s["id"] in ("vpn-vp2", "vpn-vp8"):
            socks = find_socks_port(tg, s["host"], s["ssh_port"], env.get(s["password_env"], ""))
            r = apply_socks(tg, s, env, names, socks) if socks else apply_direct(s, env, names)
        else:
            r = apply_direct(s, env, names)
            if not r.get("ok"):
                socks = find_socks_port(tg, s["host"], s["ssh_port"], env.get(s["password_env"], ""))
                if socks:
                    print(f"  retry socks {socks}", flush=True)
                    r = apply_socks(tg, s, env, names, socks)
        print(f"  {'OK' if r.get('ok') else 'FAIL'}", flush=True)
        if not r.get("ok") and r.get("error"):
            print("  " + r["error"][:300].replace("\n", " | "), flush=True)
        results.append({"id": s["id"], **r, "domains": names})
        time.sleep(0.2)

    patch_inv(servers)
    out = ROOT / "artifacts" / "extra-cdn-zones-results.json"
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    ok = sum(1 for x in results if x.get("ok"))
    print(f"Done: {ok}/{len(results)} -> {out}", flush=True)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
