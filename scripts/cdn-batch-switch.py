#!/usr/bin/env python3
"""Batch: switch listed VP servers to CDN (panel + Cloudflare). Direct SSH first, SOCKS via jump if needed."""
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
TARGETS_PATH = ROOT / "artifacts" / "cdn-targets.json"
ZONES = ["sdfsdfsdfsd.store", "losttv.site", "linkusers3.online"]
SOCKS_PORTS = [1080, 1081, 1082, 1083, 1084, 1085]
RESULTS_PATH = ROOT / "artifacts" / "cdn-batch-results.json"


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


def ensure_socks_forwards(tg: dict) -> None:
    need = []
    for port in SOCKS_PORTS:
        rc, out, _ = run(["cmd", "/c", f'netstat -ano | findstr "LISTENING" | findstr ":{port} "'], timeout=10)
        if rc != 0 or not out.strip():
            need.append(port)
    if not need:
        return
    fw = " ".join(f"-L {p}:127.0.0.1:{p}" for p in need)
    args = f'-ssh {tg["ssh_user"]}@{tg["ssh_host"]} -P 22 -pw {tg["ssh_password"]} -N {fw} -batch'
    subprocess.Popen([str(PLINK)] + args.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)


def try_direct(host: str, port: int, password: str) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh", encoding="utf-8", newline="\n") as f:
        f.write("echo DIRECT_OK; hostname\n")
        path = f.name
    try:
        rc, out, err = run(
            [str(PLINK), "-ssh", f"root@{host}", "-P", str(port), "-pw", password, "-batch", "-m", path],
            timeout=25,
        )
        text = out + err
        return ("DIRECT_OK" in text), text[-200:]
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


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
            os.unlink(path)
        except OSError:
            pass


def find_working_socks(tg: dict, host: str, port: int, password: str) -> int | None:
    pw_b64 = base64.b64encode(password.encode()).decode()
    body = f"""set +e
echo {pw_b64} | base64 -d > /tmp/cdn-batch.pass
chmod 600 /tmp/cdn-batch.pass
export SSHPASS=$(cat /tmp/cdn-batch.pass)
for p in {' '.join(str(x) for x in SOCKS_PORTS)}; do
  out=$(timeout 18 sshpass -e ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/cdn-batch.known -o ConnectTimeout=12 -o ProxyCommand="nc -X 5 -x 127.0.0.1:$p %h %p" -p {port} root@{host} 'echo SOCKS_OK' 2>/dev/null) || true
  if echo "$out" | grep -q SOCKS_OK; then
    echo FOUND $p
    exit 0
  fi
done
echo NONE
exit 1
"""
    rc, text = jump_run(tg, body, timeout=180)
    m = re.search(r"FOUND (\d+)", text)
    return int(m.group(1)) if m else None


def panel_script(domains: list[str]) -> str:
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
            print("MISSING", name)
            continue
        old = str(row.mode)
        row.mode = DomainType.cdn
        row.sub_link_only = False
        print("MODE", name, old, "->", row.mode)
    fs = BoolConfig.query.filter_by(key="first_setup").first()
    if fs is not None:
        fs.value = False
    db.session.commit()
    for d in Domain.query.order_by(Domain.id).all():
        print("DOMAIN", d.domain, d.mode)
print("PANEL_OK")
"""


def domains_for_cdn(server: dict) -> list[str]:
    """CDN-switch inventory domains for this server subdomain (+ vp5 on vp4)."""
    sub = server["subdomain"]
    out = []
    for d in server.get("domains") or []:
        # skip if looks like IP
        if re.match(r"^\d+\.\d+\.\d+\.\d+", d):
            continue
        out.append(d)
    # ensure primary subdomain zones present
    for z in ZONES:
        name = f"{sub}.{z}"
        if name not in out:
            out.append(name)
    return out


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


def cloudflare_proxied(token: str, names: list[str], content_ip: str) -> list[str]:
    logs = []
    zone_ids = {}
    for z in ZONES:
        r = cf_api(token, "GET", f"/zones?name={urllib.parse.quote(z)}")
        zone_ids[z] = r["result"][0]["id"]
    for name in names:
        zone = next((z for z in ZONES if name.endswith("." + z)), None)
        if not zone:
            logs.append(f"skip {name}: unknown zone")
            continue
        zid = zone_ids[zone]
        q = urllib.parse.urlencode({"type": "A", "name": name})
        recs = cf_api(token, "GET", f"/zones/{zid}/dns_records?{q}")["result"]
        body = {"type": "A", "name": name, "content": content_ip, "proxied": True, "ttl": 1}
        if not recs:
            cr = cf_api(token, "POST", f"/zones/{zid}/dns_records", body)
            logs.append(f"CREATE {name} proxied={cr['result'].get('proxied')}")
        else:
            for rec in recs:
                ur = cf_api(token, "PUT", f"/zones/{zid}/dns_records/{rec['id']}", body)
                logs.append(f"UPDATE {name} {rec.get('proxied')}->{ur['result'].get('proxied')}")
    return logs


def run_panel_via_direct(host: str, port: int, password: str, domains: list[str]) -> tuple[bool, str]:
    py = panel_script(domains)
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".py", encoding="utf-8", newline="\n") as f:
        f.write(py)
        local_py = f.name
    remote = f"""set -euo pipefail
install -d -m 700 /var/backups/hiddify
cd /opt/hiddify-manager/hiddify-panel
out=$(/opt/hiddify-manager/.venv313/bin/hiddifypanel backup)
src=$(printf '%s\\n' "$out" | awk '/^backup\\/.*\\.json$/ {{print; exit}}')
test -n "$src"
cp -a "/opt/hiddify-manager/hiddify-panel/$src" /var/backups/hiddify/pre-cdn-batch.json
chmod 600 /var/backups/hiddify/pre-cdn-batch.json
echo BACKUP_OK
/opt/hiddify-manager/.venv313/bin/python /tmp/cdn-panel.py
echo APPLY_START
/opt/hiddify-manager/apply_configs.sh --no-gui --no-log
echo APPLY_DONE
/opt/hiddify-manager/.venv313/bin/hiddifypanel set-setting -k first_setup -v false >/dev/null 2>&1 || true
echo ALL_OK
"""
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh", encoding="utf-8", newline="\n") as f:
        f.write(remote)
        local_sh = f.name
    try:
        # upload py via plink echo base64 to avoid pscp hostkey issues where possible
        b64 = base64.b64encode(Path(local_py).read_bytes()).decode()
        up = f"echo {b64} | base64 -d > /tmp/cdn-panel.py\n"
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh", encoding="utf-8", newline="\n") as f:
            f.write(up + open(local_sh, encoding="utf-8").read())
            combined = f.name
        rc, out, err = run(
            [str(PLINK), "-ssh", f"root@{host}", "-P", str(port), "-pw", password, "-batch", "-m", combined],
            timeout=900,
        )
        text = out + err
        return ("ALL_OK" in text), text[-2500:]
    finally:
        for p in (local_py, local_sh):
            try:
                os.unlink(p)
            except OSError:
                pass


def run_panel_via_socks(tg: dict, host: str, port: int, password: str, socks: int, domains: list[str]) -> tuple[bool, str]:
    py_b64 = base64.b64encode(panel_script(domains).encode()).decode()
    pw_b64 = base64.b64encode(password.encode()).decode()
    body = f"""set -euo pipefail
echo {pw_b64} | base64 -d > /tmp/cdn-batch.pass
chmod 600 /tmp/cdn-batch.pass
export SSHPASS=$(cat /tmp/cdn-batch.pass)
PROXY='nc -X 5 -x 127.0.0.1:{socks} %h %p'
echo {py_b64} | base64 -d > /tmp/cdn-panel.py
sshpass -e scp -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/cdn-batch.known -o ConnectTimeout=25 -o ProxyCommand="$PROXY" -P {port} /tmp/cdn-panel.py root@{host}:/tmp/cdn-panel.py
sshpass -e ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/cdn-batch.known -o ConnectTimeout=25 -o ProxyCommand="$PROXY" -p {port} root@{host} 'set -euo pipefail
install -d -m 700 /var/backups/hiddify
cd /opt/hiddify-manager/hiddify-panel
out=$(/opt/hiddify-manager/.venv313/bin/hiddifypanel backup)
src=$(printf "%s\\n" "$out" | awk "/^backup\\/.*\\.json$/ {{print; exit}}")
test -n "$src"
cp -a "/opt/hiddify-manager/hiddify-panel/$src" /var/backups/hiddify/pre-cdn-batch.json
chmod 600 /var/backups/hiddify/pre-cdn-batch.json
echo BACKUP_OK
/opt/hiddify-manager/.venv313/bin/python /tmp/cdn-panel.py
echo APPLY_START
/opt/hiddify-manager/apply_configs.sh --no-gui --no-log
echo APPLY_DONE
/opt/hiddify-manager/.venv313/bin/hiddifypanel set-setting -k first_setup -v false >/dev/null 2>&1 || true
echo ALL_OK'
"""
    rc, text = jump_run(tg, body, timeout=1000)
    return ("ALL_OK" in text), text[-2500:]


def update_inventory(server_ids: list[str]) -> None:
    path = ROOT / "inventory.yml"
    text = path.read_text(encoding="utf-8", errors="replace")
    for sid in server_ids:
        m = re.search(rf"(?ms)(^  - id: {re.escape(sid)}\n)(.*?)(?=^  - id: |\Z)", text)
        if not m:
            continue
        head, body = m.group(1), m.group(2)
        body2 = re.sub(r"(?m)^(\s+domain_modes:\s*)\[[^\]]*\]", r"\1[cdn]", body, count=1)
        note = 'status_note: "domains CDN proxied"'
        if re.search(r"(?m)^\s+status_note:", body2):
            body2 = re.sub(r"(?m)^\s+status_note:.*$", f"    {note}", body2, count=1)
        else:
            body2 = body2.rstrip() + f"\n    {note}\n"
        text = text[: m.start()] + head + body2 + text[m.end() :]
    path.write_text(text, encoding="utf-8")


def main() -> int:
    env = load_dotenv()
    tg = load_tg()
    token = env.get("CLOUDFLARE_API_TOKEN", "")
    if not token:
        print("missing CLOUDFLARE_API_TOKEN", file=sys.stderr)
        return 2
    ensure_socks_forwards(tg)
    servers = json.loads(TARGETS_PATH.read_text(encoding="utf-8"))
    results = []

    for i, s in enumerate(servers, 1):
        sid = s["id"]
        host = s["host"]
        port = int(s.get("ssh_port") or 22)
        pe = s.get("password_env")
        password = env.get(pe or "", "")
        domains = domains_for_cdn(s)
        print(f"\n[{i}/{len(servers)}] {sid} {host}:{port} domains={len(domains)}", flush=True)
        rec = {
            "id": sid,
            "host": host,
            "ssh_port": port,
            "direct_ok": False,
            "access": "none",
            "socks_port": None,
            "panel_ok": False,
            "cf_ok": False,
            "error": None,
            "cf_log": [],
            "detail": "",
        }
        if not password:
            rec["error"] = f"missing {pe}"
            results.append(rec)
            print("  FAIL no password", flush=True)
            continue

        # 1) CF first (no SSH needed)
        try:
            rec["cf_log"] = cloudflare_proxied(token, domains, host)
            rec["cf_ok"] = True
            print("  CF ok", flush=True)
        except Exception as e:
            rec["error"] = f"cf: {e}"
            print("  CF FAIL", e, flush=True)

        # 2) SSH direct
        dok, dmsg = try_direct(host, port, password)
        rec["direct_ok"] = dok
        if dok:
            print("  SSH direct ok", flush=True)
            pok, pmsg = run_panel_via_direct(host, port, password, domains)
            rec["access"] = "direct"
            rec["panel_ok"] = pok
            rec["detail"] = pmsg[-800:]
            print("  PANEL", "ok" if pok else "FAIL", flush=True)
        else:
            print("  SSH direct unavailable, trying SOCKS...", flush=True)
            socks = find_working_socks(tg, host, port, password)
            if socks is None:
                rec["access"] = "unavailable"
                rec["error"] = (rec.get("error") or "") + "; ssh direct+socks fail"
                print("  SSH SOCKS unavailable", flush=True)
            else:
                rec["socks_port"] = socks
                rec["access"] = f"socks:{socks}"
                print(f"  SSH via SOCKS {socks}", flush=True)
                pok, pmsg = run_panel_via_socks(tg, host, port, password, socks, domains)
                rec["panel_ok"] = pok
                rec["detail"] = pmsg[-800:]
                print("  PANEL", "ok" if pok else "FAIL", flush=True)

        results.append(rec)
        RESULTS_PATH.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    ok_ids = [r["id"] for r in results if r.get("panel_ok") and r.get("cf_ok")]
    update_inventory(ok_ids)

    print("\n=== SUMMARY ===", flush=True)
    print(f"{'id':<12} {'access':<14} {'direct':<7} {'panel':<6} {'cf':<4} err", flush=True)
    for r in results:
        print(
            f"{r['id']:<12} {r['access']:<14} {str(r['direct_ok']):<7} {str(r['panel_ok']):<6} {str(r['cf_ok']):<4} {r.get('error') or ''}",
            flush=True,
        )
    print(f"Wrote {RESULTS_PATH}", flush=True)
    return 0 if all(r.get("panel_ok") and r.get("cf_ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
