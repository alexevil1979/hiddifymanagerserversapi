#!/usr/bin/env python3
"""Install Hiddify panel backup → Telegram on fleet servers (idempotent)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FVS_BACKUP = Path(r"C:\Users\1\Documents\fullvpnservice\scripts\hiddify-backup")
PLINK = Path(r"C:\Program Files\PuTTY\plink.exe")
PSCP = Path(r"C:\Program Files\PuTTY\pscp.exe")
INSTALL_DIR = "/opt/hiddify-manager/scripts/backup-telegram"
CRON_FILE = "/etc/cron.d/hiddify-backup-telegram"
SCRIPT_NAME = "send-backup-to-telegram.sh"


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
        sub = re.search(r"(?m)^\s+subdomain:\s*(\S+)", p)
        sport = re.search(r"(?m)^\s+ssh_port:\s*(\d+)", p)
        servers.append(
            {
                "id": m_id.group(1),
                "host": m_host.group(1),
                "method": auth.group(1) if auth else "password",
                "password_env": pe.group(1) if pe else None,
                "key_path_env": ke.group(1) if ke else None,
                "subdomain": sub.group(1) if sub else m_id.group(1),
                "ssh_port": int(sport.group(1)) if sport else 22,
            }
        )
    return servers


def run(cmd: list[str], timeout: int = 120, input_text: str | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd,
            input=input_text,
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


def ssh_base(server: dict, env: dict) -> list[str] | None:
    host = server["host"]
    method = server["method"]
    base = [str(PLINK), "-ssh", f"root@{host}", "-P", "22", "-batch"]
    if method == "key":
        key_env = server.get("key_path_env")
        key = env.get(key_env or "", "") if key_env else ""
        if not key or not Path(key).is_file():
            return None
        base += ["-i", key]
    elif method == "password":
        pe = server.get("password_env")
        pw = env.get(pe or "", "") if pe else ""
        if not pw:
            return None
        base += ["-pw", pw]
    else:
        return None
    return base


def pscp_base(server: dict, env: dict) -> list[str] | None:
    host = server["host"]
    method = server["method"]
    base = [str(PSCP), "-batch", "-P", "22"]
    if method == "key":
        key_env = server.get("key_path_env")
        key = env.get(key_env or "", "") if key_env else ""
        if not key or not Path(key).is_file():
            return None
        base += ["-i", key]
    elif method == "password":
        pe = server.get("password_env")
        pw = env.get(pe or "", "") if pe else ""
        if not pw:
            return None
        base += ["-pw", pw]
    else:
        return None
    return base


def install_bash(token: str, chat_id: str, label: str) -> str:
    # token/chat_id injected by Python; single-quoted heredoc on remote keeps values literal
    return f"""set -euo pipefail
INSTALL_DIR='{INSTALL_DIR}'
mkdir -p "$INSTALL_DIR" /opt/hiddify-manager/log
chmod 755 "$INSTALL_DIR/{SCRIPT_NAME}"
cat > "$INSTALL_DIR/.env" <<'EOF'
TELEGRAM_BOT_TOKEN={token}
TELEGRAM_CHAT_ID={chat_id}
SERVER_LABEL="{label}"
HIDDIFY_BACKUP_DIR=/opt/hiddify-manager/hiddify-panel/backup
TELEGRAM_MAX_BYTES=49283072
MAX_BACKUP_AGE_HOURS=24
EOF
chmod 600 "$INSTALL_DIR/.env"
cat > '{CRON_FILE}' <<'EOF'
# Hiddify: daily panel backup archive → admin Telegram
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
30 3 * * * root {INSTALL_DIR}/{SCRIPT_NAME} >> /opt/hiddify-manager/log/hiddify-backup-telegram.cron.log 2>&1
EOF
chmod 644 '{CRON_FILE}'
if command -v hiddifypanel >/dev/null 2>&1; then
  hiddifypanel set-setting -k telegram_bot_token -v '{token}' >/dev/null 2>&1 || true
fi
test -x "$INSTALL_DIR/{SCRIPT_NAME}"
test -f "$INSTALL_DIR/.env"
grep -q 'TELEGRAM_CHAT_ID={chat_id}' "$INSTALL_DIR/.env"
echo OK
"""


def deploy_direct(server: dict, env: dict, token: str, chat_id: str, do_test: bool) -> dict:
    sid = server["id"]
    host = server["host"]
    label = f"{sid} {server.get('subdomain') or sid}"
    sb = ssh_base(server, env)
    pb = pscp_base(server, env)
    if not sb or not pb:
        return {"id": sid, "host": host, "ok": False, "error": "missing auth secret"}

    script_src = FVS_BACKUP / SCRIPT_NAME
    run(sb + [f"mkdir -p {INSTALL_DIR}"], timeout=60)
    rc, out, err = run(
        pb + [str(script_src), f"root@{host}:{INSTALL_DIR}/{SCRIPT_NAME}"],
        timeout=90,
    )
    if rc != 0:
        return {"id": sid, "host": host, "ok": False, "error": f"pscp failed: {(err or out)[:200]}"}

    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8", newline="\n") as f:
        f.write(install_bash(token, chat_id, label))
        remote_path = f.name
    try:
        rc, out, err = run(sb + ["-m", remote_path], timeout=120)
    finally:
        try:
            os.unlink(remote_path)
        except OSError:
            pass

    if rc != 0 or "OK" not in out:
        return {
            "id": sid,
            "host": host,
            "ok": False,
            "error": f"install failed rc={rc}: {(out + err)[:300]}",
        }

    result: dict = {"id": sid, "host": host, "ok": True, "error": None}
    if do_test:
        rc2, out2, err2 = run(sb + [f"{INSTALL_DIR}/{SCRIPT_NAME}"], timeout=300)
        result["test_ok"] = rc2 == 0
        result["test_out"] = ((out2 or "") + (err2 or ""))[-500:]
    return result


def deploy_vp2_via_jump(env: dict, token: str, chat_id: str) -> dict:
    sid = "vpn-vp2"
    host = "150.241.86.99"
    label = f"{sid} vp2"
    pw = env.get("SSH_PASSWORD_VP2", "")
    if not pw:
        return {"id": sid, "host": host, "ok": False, "error": "missing SSH_PASSWORD_VP2"}

    script_src = FVS_BACKUP / SCRIPT_NAME
    script_b64 = __import__("base64").b64encode(script_src.read_bytes()).decode()
    install_b64 = __import__("base64").b64encode(install_bash(token, chat_id, label).encode()).decode()
    pw_b64 = __import__("base64").b64encode(pw.encode()).decode()

    body = f"""set -euo pipefail
echo {pw_b64} | base64 -d > /tmp/vp2.pass
chmod 600 /tmp/vp2.pass
echo {script_b64} | base64 -d > /tmp/{SCRIPT_NAME}
echo {install_b64} | base64 -d > /tmp/install-backup-tg.sh
chmod +x /tmp/{SCRIPT_NAME} /tmp/install-backup-tg.sh
/tmp/vp2-ssh.sh 1084 "mkdir -p {INSTALL_DIR}"
/tmp/vp2-scp.sh 1084 /tmp/{SCRIPT_NAME} root@{host}:{INSTALL_DIR}/{SCRIPT_NAME}
/tmp/vp2-scp.sh 1084 /tmp/install-backup-tg.sh root@{host}:/tmp/install-backup-tg.sh
/tmp/vp2-ssh.sh 1084 'bash /tmp/install-backup-tg.sh; rm -f /tmp/install-backup-tg.sh'
rm -f /tmp/{SCRIPT_NAME} /tmp/install-backup-tg.sh
echo JUMP_OK
"""
    rc, text = jump_run(body, timeout=240)
    ok = rc == 0 and "JUMP_OK" in text and "OK" in text
    return {
        "id": sid,
        "host": host,
        "ok": ok,
        "error": None if ok else f"jump deploy failed: {text[-400:]}",
    }


def main() -> int:
    env = load_dotenv(ROOT / ".env")
    token = env.get("HIDDIFY_BACKUP_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = env.get("HIDDIFY_BACKUP_TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Missing HIDDIFY_BACKUP_TELEGRAM_* in .env", file=sys.stderr)
        return 2

    servers = parse_inventory(ROOT / "inventory.yml")
    only = set(sys.argv[1:]) if len(sys.argv) > 1 else set()
    if only:
        servers = [s for s in servers if s["id"] in only or s.get("subdomain") in only]

    print(f"Servers: {len(servers)} chat_id={chat_id}", flush=True)
    results = []
    test_id = "vpn-vp4"

    for i, s in enumerate(servers, 1):
        print(f"[{i}/{len(servers)}] {s['id']} {s['host']} ...", flush=True)
        if s["id"] == "vpn-vp2":
            r = deploy_vp2_via_jump(env, token, chat_id)
        else:
            r = deploy_direct(s, env, token, chat_id, do_test=(s["id"] == test_id))
        status = "OK" if r["ok"] else "FAIL"
        extra = ""
        if r.get("test_ok") is True:
            extra = " test=OK"
        elif r.get("test_ok") is False:
            extra = " test=FAIL"
        print(f"  {status}{extra}" + (f" {r['error']}" if r.get("error") else ""), flush=True)
        if r.get("test_out"):
            safe = (r["test_out"][-300:]).encode("ascii", "replace").decode("ascii")
            print(f"  test_out: {safe}", flush=True)
        results.append(r)
        time.sleep(0.2)

    out_path = ROOT / "artifacts" / "backup-telegram-deploy.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    ok = sum(1 for r in results if r["ok"])
    fail = len(results) - ok
    print(f"Done: {ok} ok, {fail} fail -> {out_path}", flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
