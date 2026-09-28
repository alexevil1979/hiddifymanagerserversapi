#!/usr/bin/env python3
"""Add telegrambot.website + teleworker.fun as CDN domains for all vpn-vp* servers.

1) Cloudflare A records (proxied) for {sub}.{zone} → host
2) Panel: create/update Domain mode=cdn
3) apply_configs
4) Patch local inventory domains lists
"""
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
SSH = Path(r"C:\Windows\System32\OpenSSH\ssh.exe")
SCP = Path(r"C:\Windows\System32\OpenSSH\scp.exe")

NEW_ZONES = ["telegrambot.website", "teleworker.fun"]
SOCKS_PORTS = [1080, 1081, 1082, 1083, 1084, 1085]
RESULTS = ROOT / "artifacts" / "extra-cdn-zones-results.json"


def load_dotenv() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_tg() -> dict:
    return json.loads((ROOT / "telegram.local.json").read_text(encoding="utf-8"))


def parse_inventory(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    parts = re.split(r"(?m)^(?=  - id:)", text)
    servers: list[dict] = []
    for p in parts[1:]:
        m_id = re.search(r"(?m)^\s+- id:\s*(\S+)", p)
        m_host = re.search(r"(?m)^\s+host:\s*(\S+)", p)
        if not m_id or not m_host:
            continue
        sid = m_id.group(1)
        if not sid.startswith("vpn-vp"):
            continue
        auth = re.search(r"(?m)^\s+method:\s*(\S+)", p)
        pe = re.search(r"(?m)^\s+password_env:\s*(\S+)", p)
        ke = re.search(r"(?m)^\s+key_path_env:\s*(\S+)", p)
        sub = re.search(r"(?m)^\s+subdomain:\s*(\S+)", p)
        sport = re.search(r"(?m)^\s+ssh_port:\s*(\d+)", p)
        suser = re.search(r"(?m)^\s+ssh_user:\s*(\S+)", p)
        domains = re.findall(r"(?m)^\s+- ((?:[a-zA-Z0-9-]+\.)+[a-zA-Z0-9.-]+)\s*$", p)
        # only top-level domains list under card — filter noise later
        servers.append(
            {
                "id": sid,
                "host": m_host.group(1),
                "method": auth.group(1) if auth else "password",
                "password_env": pe.group(1) if pe else None,
                "key_path_env": ke.group(1) if ke else None,
                "subdomain": sub.group(1) if sub else sid.replace("vpn-", ""),
                "ssh_port": int(sport.group(1)) if sport else 22,
                "ssh_user": suser.group(1) if suser else "root",
                "domains": domains,
            }
        )
    return servers


def run(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", (e.stderr or "") + "\nTIMEOUT"


def jump_run(tg: dict, body: str, timeout: int = 900) -> tuple[int, str]:
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh", encoding="utf-8", newline="\n") as f:
        f.write(body.replace("\r\n", "\n").replace("\r", "\n"))
        path = f.name
    try:
        rc, out, err = run(
            [
                str(PLINK),
                "-ssh",
                "-batch",
                "-pw",
                tg["ssh_password"],
                "-m",
                path,
                f"{tg['ssh_user']}@{tg['ssh_host']}",
            ],
            timeout=timeout,
        )
        return rc, (out or "") + (err or "")
    finally:
        try:
            Path(path).unlink()
        except OSError:
            pass


def cf_api(token: str, method: str, path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def ensure_cf_records(token: str, sub: str, ip: str) -> list[str]:
    logs = []
    zone_ids = {}
    for z in NEW_ZONES:
        r = cf_api(token, "GET", f"/zones?name={urllib.parse.quote(z)}")
        if not r.get("result"):
            logs.append(f"ZONE_MISSING {z}")
            continue
        zone_ids[z] = r["result"][0]["id"]
    for z, zid in zone_ids.items():
        name = f"{sub}.{z}"
        q = urllib.parse.urlencode({"type": "A", "name": name})
        recs = cf_api(token, "GET", f"/zones/{zid}/dns_records?{q}")["result"]
        body = {"type": "A", "name": name, "content": ip, "proxied": True, "ttl": 1}
        if not recs:
            cr = cf_api(token, "POST", f"/zones/{zid}/dns_records", body)
            ok = cr.get("success")
            logs.append(f"CREATE {name} -> {ip} proxied={cr.get('result', {}).get('proxied')} ok={ok}")
        else:
            rec = recs[0]
            need = (rec.get("content") != ip) or (rec.get("proxied") is not True)
            if need:
                ur = cf_api(token, "PUT", f"/zones/{zid}/dns_records/{rec['id']}", body)
                logs.append(
                    f"UPDATE {name} {rec.get('content')}/{rec.get('proxied')} -> {ip}/True ok={ur.get('success')}"
                )
            else:
                logs.append(f"OK {name} -> {ip} proxied=True")
    return logs


def panel_add_cdn_script(domains: list[str]) -> str:
    names = json.dumps(domains)
    return f"""#!/usr/bin/env python3
import os
from pathlib import Path
os.chdir("/opt/hiddify-manager/hiddify-panel")
for line in Path("app.cfg").read_text().splitlines():
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ[k.strip()] = v.strip().strip("'").strip('"')
from hiddifypanel.base import create_app
app = create_app(app_mode="cli")
names = {names}
with app.app_context():
    from hiddifypanel.database import db
    from hiddifypanel.models import Domain, DomainType, BoolConfig
    for name in names:
        row = Domain.query.filter_by(domain=name).first()
        if not row:
            row = Domain(domain=name, mode=DomainType.cdn)
            db.session.add(row)
            print("ADD", name, "cdn")
        else:
            old = str(row.mode)
            row.mode = DomainType.cdn
            print("MODE", name, old, "->", row.mode)
        if hasattr(row, "sub_link_only"):
            row.sub_link_only = False
    fs = BoolConfig.query.filter_by(key="first_setup").first()
    if fs is not None:
        fs.value = False
    db.session.commit()
    for d in Domain.query.order_by(Domain.id).all():
        print("DOMAIN", d.domain, d.mode)
print("PANEL_OK")
"""


def remote_apply_body() -> str:
    return """set -euo pipefail
install -d -m 700 /var/backups/hiddify
cd /opt/hiddify-manager/hiddify-panel
out=$(/opt/hiddify-manager/.venv313/bin/hiddifypanel backup)
src=$(printf '%s\\n' "$out" | awk '/^backup\\/.*\\.json$/ {print; exit}')
test -n "$src"
cp -a "/opt/hiddify-manager/hiddify-panel/$src" /var/backups/hiddify/pre-extra-cdn-zones.json
chmod 600 /var/backups/hiddify/pre-extra-cdn-zones.json
echo BACKUP_OK
/opt/hiddify-manager/.venv313/bin/python /tmp/extra-cdn-panel.py
echo APPLY_START
/opt/hiddify-manager/apply_configs.sh --no-gui --no-log
echo APPLY_DONE
/opt/hiddify-manager/.venv313/bin/hiddifypanel set-setting -k first_setup -v false >/dev/null 2>&1 || true
echo ALL_OK
"""


def deploy_direct_password(server: dict, env: dict, domains: list[str]) -> dict:
    host = server["host"]
    port = server["ssh_port"]
    pe = server.get("password_env")
    pw = env.get(pe or "", "") if pe else ""
    if not pw:
        return {"ok": False, "error": "missing password"}
    py_b64 = base64.b64encode(panel_add_cdn_script(domains).encode()).decode()
    body = f"echo {py_b64} | base64 -d > /tmp/extra-cdn-panel.py\n" + remote_apply_body()
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh", encoding="utf-8", newline="\n") as f:
        f.write(body)
        path = f.name
    try:
        rc, out, err = run(
            [str(PLINK), "-ssh", f"root@{host}", "-P", str(port), "-pw", pw, "-batch", "-m", path],
            timeout=1200,
        )
        text = out + err
        return {"ok": "ALL_OK" in text, "error": None if "ALL_OK" in text else text[-1500:], "log": text[-800:]}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def deploy_via_socks(tg: dict, server: dict, env: dict, domains: list[str], socks: int) -> dict:
    host = server["host"]
    port = server["ssh_port"]
    pe = server.get("password_env")
    pw = env.get(pe or "", "") if pe else ""
    if not pw:
        return {"ok": False, "error": "missing password"}
    py_b64 = base64.b64encode(panel_add_cdn_script(domains).encode()).decode()
    apply_b64 = base64.b64encode(remote_apply_body().encode()).decode()
    pw_b64 = base64.b64encode(pw.encode()).decode()
    body = f"""set -euo pipefail
echo {pw_b64} | base64 -d > /tmp/extra-cdn.pass
chmod 600 /tmp/extra-cdn.pass
export SSHPASS=$(cat /tmp/extra-cdn.pass)
PROXY='nc -X 5 -x 127.0.0.1:{socks} %h %p'
echo {py_b64} | base64 -d > /tmp/extra-cdn-panel.py
echo {apply_b64} | base64 -d > /tmp/extra-cdn-apply.sh
chmod +x /tmp/extra-cdn-apply.sh
sshpass -e scp -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/extra-cdn.known -o ConnectTimeout=25 -o ProxyCommand="$PROXY" -P {port} /tmp/extra-cdn-panel.py /tmp/extra-cdn-apply.sh root@{host}:/tmp/
sshpass -e ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/extra-cdn.known -o ConnectTimeout=25 -o ProxyCommand="$PROXY" -p {port} root@{host} 'bash /tmp/extra-cdn-apply.sh'
echo JUMP_DONE
"""
    rc, text = jump_run(tg, body, timeout=1200)
    ok = "ALL_OK" in text
    return {"ok": ok, "error": None if ok else text[-1500:], "log": text[-800:], "via": f"socks{socks}"}


def find_socks(tg: dict, host: str, port: int, password: str) -> int | None:
    pw_b64 = base64.b64encode(password.encode()).decode()
    body = f"""set +e
echo {pw_b64} | base64 -d > /tmp/extra-cdn.pass
chmod 600 /tmp/extra-cdn.pass
export SSHPASS=$(cat /tmp/extra-cdn.pass)
for p in {' '.join(str(x) for x in SOCKS_PORTS)}; do
  out=$(timeout 18 sshpass -e ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/extra-cdn.known -o ConnectTimeout=12 -o ProxyCommand="nc -X 5 -x 127.0.0.1:$p %h %p" -p {port} root@{host} 'echo SOCKS_OK' 2>/dev/null) || true
  if echo "$out" | grep -q SOCKS_OK; then echo FOUND $p; exit 0; fi
done
echo NONE; exit 1
"""
    rc, text = jump_run(tg, body, timeout=180)
    m = re.search(r"FOUND (\d+)", text)
    return int(m.group(1)) if m else None


def patch_inventory(servers: list[dict]) -> None:
    path = ROOT / "inventory.yml"
    text = path.read_text(encoding="utf-8")
    for s in servers:
        sid = s["id"]
        sub = s["subdomain"]
        new_names = [f"{sub}.{z}" for z in NEW_ZONES]
        idx = text.find(f"id: {sid}")
        if idx < 0:
            continue
        end = text.find("\n  - id:", idx + 1)
        if end < 0:
            end = len(text)
        chunk = text[idx:end]
        # ensure domains list contains new names
        if "domains:" not in chunk:
            continue
        for name in new_names:
            if name in chunk:
                continue
            # insert after domains: line first entry area — append before blank line after domains list
            m = re.search(r"(?ms)^(    domains:\n(?:      - .+\n)+)", chunk)
            if not m:
                continue
            block = m.group(1)
            if f"      - {name}\n" not in block:
                block2 = block.rstrip("\n") + f"\n      - {name}\n"
                chunk = chunk.replace(block, block2, 1)
        # status note
        note = f'status_note: "extra CDN zones {", ".join(NEW_ZONES)}"'
        if re.search(r"(?m)^    status_note:", chunk):
            chunk = re.sub(r"(?m)^    status_note:.*$", note, chunk, count=1)
        else:
            chunk = re.sub(r"(?m)^(    state:)", note + r"\n\1", chunk, count=1)
        chunk = re.sub(
            r"(?m)^    status_updated_at:.*$",
            'status_updated_at: "2026-09-28 18:50"',
            chunk,
            count=1,
        )
        text = text[:idx] + chunk + text[end:]
    path.write_text(text, encoding="utf-8")


def main() -> int:
    env = load_dotenv()
    tg = load_tg()
    token = env.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not token:
        print("missing CLOUDFLARE_API_TOKEN", file=sys.stderr)
        return 2

    servers = parse_inventory(ROOT / "inventory.yml")
    only = set(a for a in sys.argv[1:] if not a.startswith("--"))
    dry = "--dry-run" in sys.argv
    skip_apply = "--dns-only" in sys.argv
    if only:
        servers = [s for s in servers if s["id"] in only or s["subdomain"] in only]

    print(f"Servers: {len(servers)} zones={NEW_ZONES} dry={dry} dns_only={skip_apply}", flush=True)
    results = []

    for i, s in enumerate(servers, 1):
        sub = s["subdomain"]
        names = [f"{sub}.{z}" for z in NEW_ZONES]
        print(f"[{i}/{len(servers)}] {s['id']} {s['host']} {names}", flush=True)
        row: dict = {"id": s["id"], "host": s["host"], "domains": names}

        try:
            row["cf"] = ensure_cf_records(token, sub, s["host"])
            print("  CF:", "; ".join(row["cf"]), flush=True)
        except Exception as e:
            row["ok"] = False
            row["error"] = f"cf: {e}"
            print(f"  FAIL CF {e}", flush=True)
            results.append(row)
            continue

        if dry or skip_apply:
            row["ok"] = True
            row["panel"] = "skipped"
            results.append(row)
            continue

        # panel + apply
        pe = s.get("password_env")
        pw = env.get(pe or "", "") if pe else ""
        if s["id"] in ("vpn-vp2", "vpn-vp8") or not pw:
            socks = find_socks(tg, s["host"], s["ssh_port"], pw) if pw else None
            if socks:
                r = deploy_via_socks(tg, s, env, names, socks)
            else:
                # try direct anyway
                r = deploy_direct_password(s, env, names)
                if not r.get("ok") and pw:
                    socks = find_socks(tg, s["host"], s["ssh_port"], pw)
                    r = deploy_via_socks(tg, s, env, names, socks) if socks else r
        else:
            r = deploy_direct_password(s, env, names)
            if not r.get("ok"):
                socks = find_socks(tg, s["host"], s["ssh_port"], pw)
                if socks:
                    print(f"  retry via socks {socks}", flush=True)
                    r = deploy_via_socks(tg, s, env, names, socks)

        row.update(r)
        print(f"  PANEL {'OK' if r.get('ok') else 'FAIL'}" + (f" {r.get('error','')[:200]}" if not r.get("ok") else ""), flush=True)
        results.append(row)
        time.sleep(0.3)

    if not dry:
        patch_inventory(servers)

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    ok = sum(1 for r in results if r.get("ok"))
    fail = len(results) - ok
    print(f"Done: {ok} ok, {fail} fail -> {RESULTS}", flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
