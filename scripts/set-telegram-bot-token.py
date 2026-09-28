#!/usr/bin/env python3
"""Set Hiddify panel telegram_bot_token (admin UI «Токен Telegram bot») on fleet."""
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


def load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def parse_inventory(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    parts = re.split(r"(?m)^(?=  - id:)", text)
    servers: list[dict] = []
    for p in parts[1:]:
        if re.match(r"^\s*P\d", p):
            break
        m_id = re.search(r"(?m)^\s+- id:\s*(\S+)", p)
        m_host = re.search(r"(?m)^\s+host:\s*(\S+)", p)
        if not m_id or not m_host:
            continue
        auth = re.search(r"(?m)^\s+method:\s*(\S+)", p)
        pe = re.search(r"(?m)^\s+password_env:\s*(\S+)", p)
        ke = re.search(r"(?m)^\s+key_path_env:\s*(\S+)", p)
        user = re.search(r"(?m)^\s+ssh_user:\s*(\S+)", p)
        sport = re.search(r"(?m)^\s+ssh_port:\s*(\d+)", p)
        servers.append(
            {
                "id": m_id.group(1),
                "host": m_host.group(1),
                "method": auth.group(1) if auth else "password",
                "password_env": pe.group(1) if pe else None,
                "key_path_env": ke.group(1) if ke else None,
                "ssh_user": user.group(1) if user else "root",
                "ssh_port": int(sport.group(1)) if sport else 22,
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


def load_tg() -> dict:
    return json.loads((ROOT / "telegram.local.json").read_text(encoding="utf-8"))


def jump_run(body: str, timeout: int = 180) -> tuple[int, str]:
    tg = load_tg()
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


def set_token_remote_cmd(token: str) -> str:
    tok_b64 = base64.b64encode(token.encode()).decode()
    return f"""set -euo pipefail
TOK=$(echo '{tok_b64}' | base64 -d)
CLI=""
PYBIN=""
if [ -x /opt/hiddify-manager/.venv313/bin/hiddifypanel ]; then
  CLI=/opt/hiddify-manager/.venv313/bin/hiddifypanel
  PYBIN=/opt/hiddify-manager/.venv313/bin/python
elif [ -x /opt/hiddify-manager/.venv/bin/hiddifypanel ]; then
  CLI=/opt/hiddify-manager/.venv/bin/hiddifypanel
  PYBIN=/opt/hiddify-manager/.venv/bin/python
fi
if [ -z "$CLI" ]; then
  echo "NO_HIDDIFY_CLI" >&2
  exit 3
fi
cd /opt/hiddify-manager/hiddify-panel
"$CLI" set-setting -k telegram_bot_token -v "$TOK"
"$PYBIN" - <<PY
import os
from pathlib import Path
tok_prefix = __import__("base64").b64decode("{tok_b64}").decode()[:16]
os.chdir("/opt/hiddify-manager/hiddify-panel")
for line in Path("app.cfg").read_text().splitlines():
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ[k.strip()] = v.strip().strip("'").strip('"')
from hiddifypanel.base import create_app
app = create_app(app_mode="cli")
with app.app_context():
    from hiddifypanel.models import ConfigEnum, StrConfig
    row = StrConfig.query.filter_by(key=ConfigEnum.telegram_bot_token).first()
    val = (row.value if row else "") or ""
    if val.startswith(tok_prefix):
        print("VERIFY_OK")
    else:
        raise SystemExit(f"VERIFY_FAIL len={{len(val)}}")
PY
echo OK
"""


def deploy_openssh(server: dict, env: dict, token: str) -> dict:
    sid, host = server["id"], server["host"]
    user = server["ssh_user"]
    port = str(server["ssh_port"])
    key_env = server.get("key_path_env")
    key = env.get(key_env or "", "") if key_env else ""
    if not key or not Path(key).is_file():
        return {"id": sid, "host": host, "ok": False, "error": "missing key"}

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh", encoding="utf-8", newline="\n") as f:
        f.write(set_token_remote_cmd(token))
        local_sh = f.name
    remote_sh = f"/tmp/set-tg-token-{sid}.sh"
    try:
        rc, out, err = run(
            [
                "scp",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-i",
                key,
                "-P",
                port,
                local_sh,
                f"{user}@{host}:{remote_sh}",
            ],
            timeout=60,
        )
        if rc != 0:
            return {"id": sid, "host": host, "ok": False, "error": f"scp: {(err or out)[:200]}"}

        sudo = "sudo -n " if user != "root" else ""
        rc2, out2, err2 = run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-i",
                key,
                "-p",
                port,
                f"{user}@{host}",
                f"{sudo}bash {remote_sh}; {sudo}rm -f {remote_sh}",
            ],
            timeout=120,
        )
        text = out2 + err2
        ok = rc2 == 0 and "OK" in text
        return {"id": sid, "host": host, "ok": ok, "error": None if ok else text[-300:]}
    finally:
        try:
            os.unlink(local_sh)
        except OSError:
            pass


def deploy_plink(server: dict, env: dict, token: str) -> dict:
    sid, host = server["id"], server["host"]
    user = server.get("ssh_user") or "root"
    port = str(server.get("ssh_port") or 22)
    pe = server.get("password_env")
    pw = env.get(pe or "", "") if pe else ""
    if not pw:
        return {"id": sid, "host": host, "ok": False, "error": "missing password"}

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh", encoding="utf-8", newline="\n") as f:
        f.write(set_token_remote_cmd(token))
        local_sh = f.name
    try:
        rc, out, err = run(
            [str(PLINK), "-ssh", f"{user}@{host}", "-P", port, "-pw", pw, "-batch", "-m", local_sh],
            timeout=120,
        )
        text = out + err
        ok = rc == 0 and "OK" in text
        return {"id": sid, "host": host, "ok": ok, "error": None if ok else text[-300:]}
    finally:
        try:
            os.unlink(local_sh)
        except OSError:
            pass


def deploy_vp2(env: dict, token: str) -> dict:
    sid, host = "vpn-vp2", "150.241.86.99"
    pw = env.get("SSH_PASSWORD_VP2", "")
    if not pw:
        return {"id": sid, "host": host, "ok": False, "error": "missing SSH_PASSWORD_VP2"}
    cmd_b64 = base64.b64encode(set_token_remote_cmd(token).encode()).decode()
    pw_b64 = base64.b64encode(pw.encode()).decode()
    body = f"""set -euo pipefail
echo {pw_b64} | base64 -d > /tmp/vp2.pass
chmod 600 /tmp/vp2.pass
echo {cmd_b64} | base64 -d > /tmp/set-tg-token.sh
chmod +x /tmp/set-tg-token.sh
/tmp/vp2-scp.sh 1084 /tmp/set-tg-token.sh root@{host}:/tmp/set-tg-token.sh
/tmp/vp2-ssh.sh 1084 'bash /tmp/set-tg-token.sh; rm -f /tmp/set-tg-token.sh'
rm -f /tmp/set-tg-token.sh
echo JUMP_OK
"""
    rc, text = jump_run(body, timeout=240)
    ok = rc == 0 and "JUMP_OK" in text and "OK" in text
    return {"id": sid, "host": host, "ok": ok, "error": None if ok else text[-400:]}


def main() -> int:
    env = load_dotenv(ROOT / ".env")
    token = env.get("HIDDIFY_BACKUP_TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Missing HIDDIFY_BACKUP_TELEGRAM_BOT_TOKEN in .env", file=sys.stderr)
        return 2

    servers = parse_inventory(ROOT / "inventory.yml")
    only = set(sys.argv[1:]) if len(sys.argv) > 1 else set()
    if only:
        servers = [s for s in servers if s["id"] in only]

    print(f"Set telegram_bot_token on {len(servers)} servers", flush=True)
    results = []
    for i, s in enumerate(servers, 1):
        print(f"[{i}/{len(servers)}] {s['id']} {s['host']} user={s['ssh_user']} port={s['ssh_port']} ...", flush=True)
        if s["id"] == "vpn-vp2":
            r = deploy_vp2(env, token)
        elif s["method"] == "key":
            r = deploy_openssh(s, env, token)
        else:
            r = deploy_plink(s, env, token)
        status = "OK" if r["ok"] else "FAIL"
        print(f"  {status}" + (f" {r['error']}" if r.get("error") else ""), flush=True)
        results.append(r)
        time.sleep(0.15)

    out = ROOT / "artifacts" / "telegram-bot-token-set.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    ok = sum(1 for r in results if r["ok"])
    fail = len(results) - ok
    print(f"Done: {ok} ok, {fail} fail -> {out}", flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
