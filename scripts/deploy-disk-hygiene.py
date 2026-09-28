#!/usr/bin/env python3
"""Deploy daily disk hygiene (cleanup + Telegram report) to Hiddify fleet."""
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
SCRIPT_SRC = ROOT / "scripts" / "disk-hygiene" / "disk-hygiene.sh"
PLINK = Path(r"C:\Program Files\PuTTY\plink.exe")
PSCP = Path(r"C:\Program Files\PuTTY\pscp.exe")
INSTALL_DIR = "/opt/hiddify-manager/scripts/disk-hygiene"
CRON_FILE = "/etc/cron.d/hiddify-disk-hygiene"
SCRIPT_NAME = "disk-hygiene.sh"
SSH_EXE = Path(r"C:\Windows\System32\OpenSSH\ssh.exe")
SCP_EXE = Path(r"C:\Windows\System32\OpenSSH\scp.exe")


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


def load_tg_notify() -> dict:
    """Same bot/chat as telegram-notify.ps1 operator alerts."""
    return json.loads((ROOT / "telegram.local.json").read_text(encoding="utf-8"))


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
        suser = re.search(r"(?m)^\s+ssh_user:\s*(\S+)", p)
        servers.append(
            {
                "id": m_id.group(1),
                "host": m_host.group(1),
                "method": auth.group(1) if auth else "password",
                "password_env": pe.group(1) if pe else None,
                "key_path_env": ke.group(1) if ke else None,
                "subdomain": sub.group(1) if sub else m_id.group(1),
                "ssh_port": int(sport.group(1)) if sport else 22,
                "ssh_user": suser.group(1) if suser else "root",
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


def jump_run(body: str, timeout: int = 180) -> tuple[int, str]:
    tg = load_tg_notify()
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


def cron_minute(server_id: str) -> int:
    """Stagger fleet Telegram sends across :00–:49."""
    return sum(ord(c) for c in server_id) % 50


def ssh_base(server: dict, env: dict) -> list[str] | None:
    host = server["host"]
    port = str(server.get("ssh_port") or 22)
    method = server["method"]
    base = [str(PLINK), "-ssh", f"root@{host}", "-P", port, "-batch"]
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
    port = str(server.get("ssh_port") or 22)
    method = server["method"]
    base = [str(PSCP), "-batch", "-P", port]
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


def install_bash(token: str, chat_id: str, label: str, minute: int) -> str:
    # token/chat injected; values go into single-quoted heredoc-ish via Python formatting carefully
    # Escape single quotes in label only
    label_safe = label.replace("'", "")
    return f"""set -euo pipefail
INSTALL_DIR='{INSTALL_DIR}'
mkdir -p "$INSTALL_DIR" /opt/hiddify-manager/log /var/lib/disk-hygiene
chmod 755 "$INSTALL_DIR/{SCRIPT_NAME}"
cat > "$INSTALL_DIR/.env" <<'EOF'
TELEGRAM_BOT_TOKEN={token}
TELEGRAM_CHAT_ID={chat_id}
SERVER_LABEL='{label_safe}'
REPORT_MODE=always
WARN_PCT=80
CRIT_PCT=90
JOURNAL_MAX_SIZE=80M
JOURNAL_MAX_TIME=7d
KEEP_PANEL_BACKUPS=5
KEEP_VAR_BACKUPS=8
EOF
chmod 600 "$INSTALL_DIR/.env"
cat > '{CRON_FILE}' <<'EOF'
# Hiddify: daily disk hygiene + Telegram disk report (operator notify bot)
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
{minute} 4 * * * root {INSTALL_DIR}/{SCRIPT_NAME} >> /opt/hiddify-manager/log/disk-hygiene.cron.log 2>&1
EOF
chmod 644 '{CRON_FILE}'
test -x "$INSTALL_DIR/{SCRIPT_NAME}"
test -f "$INSTALL_DIR/.env"
grep -q 'TELEGRAM_CHAT_ID={chat_id}' "$INSTALL_DIR/.env"
echo OK
"""


def deploy_openssh_key(server: dict, env: dict, token: str, chat_id: str, do_test: bool) -> dict:
    """PuTTY cannot use OpenSSH PEM keys — use Windows OpenSSH client."""
    sid = server["id"]
    host = server["host"]
    port = str(server.get("ssh_port") or 22)
    user = server.get("ssh_user") or "root"
    label = f"{sid} {server.get('subdomain') or sid}"
    minute = cron_minute(sid)
    key_env = server.get("key_path_env")
    key = env.get(key_env or "", "") if key_env else ""
    if not key or not Path(key).is_file():
        return {"id": sid, "host": host, "ok": False, "error": "missing key file"}

    ssh_opts = [
        "-i",
        key,
        "-p",
        port,
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=20",
    ]
    target = f"{user}@{host}"
    sudo = "" if user == "root" else "sudo "

    rc, out, err = run([str(SSH_EXE), *ssh_opts, target, f"{sudo}mkdir -p {INSTALL_DIR}"], timeout=60)
    if rc != 0:
        return {"id": sid, "host": host, "ok": False, "error": f"ssh mkdir failed: {(err or out)[:200]}"}

    # scp to /tmp then move with sudo if needed
    remote_tmp_script = f"/tmp/{SCRIPT_NAME}"
    rc, out, err = run(
        [
            str(SCP_EXE),
            "-i",
            key,
            "-P",
            port,
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            str(SCRIPT_SRC),
            f"{target}:{remote_tmp_script}",
        ],
        timeout=90,
    )
    if rc != 0:
        return {"id": sid, "host": host, "ok": False, "error": f"scp failed: {(err or out)[:200]}"}

    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8", newline="\n") as f:
        f.write(install_bash(token, chat_id, label, minute))
        remote_path = f.name
    try:
        rc, out, err = run(
            [
                str(SCP_EXE),
                "-i",
                key,
                "-P",
                port,
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                remote_path,
                f"{target}:/tmp/install-disk-hygiene.sh",
            ],
            timeout=90,
        )
        if rc != 0:
            return {"id": sid, "host": host, "ok": False, "error": f"scp install failed: {(err or out)[:200]}"}
        remote_cmd = (
            f"{sudo}cp -f {remote_tmp_script} {INSTALL_DIR}/{SCRIPT_NAME}; "
            f"{sudo}bash /tmp/install-disk-hygiene.sh; "
            f"rm -f /tmp/install-disk-hygiene.sh {remote_tmp_script}"
        )
        rc, out, err = run([str(SSH_EXE), *ssh_opts, target, remote_cmd], timeout=120)
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

    result: dict = {"id": sid, "host": host, "ok": True, "error": None, "cron_minute": minute}
    if do_test:
        rc2, out2, err2 = run(
            [str(SSH_EXE), *ssh_opts, target, f"{sudo}{INSTALL_DIR}/{SCRIPT_NAME}"],
            timeout=180,
        )
        result["test_ok"] = rc2 == 0
        result["test_out"] = ((out2 or "") + (err2 or ""))[-800:]
    return result


def deploy_vp8_via_jump(env: dict, token: str, chat_id: str, do_test: bool) -> dict:
    """vp8 often needs SOCKS via jump (direct SSH timeout)."""
    sid = "vpn-vp8"
    host = "193.37.213.114"
    label = f"{sid} vp8"
    minute = cron_minute(sid)
    pw = env.get("SSH_PASSWORD_VP8", "")
    if not pw:
        return {"id": sid, "host": host, "ok": False, "error": "missing SSH_PASSWORD_VP8"}

    script_b64 = base64.b64encode(SCRIPT_SRC.read_bytes()).decode()
    install_b64 = base64.b64encode(install_bash(token, chat_id, label, minute).encode()).decode()
    pw_b64 = base64.b64encode(pw.encode()).decode()
    test_cmd = (
        f"/tmp/vp2-ssh.sh 1084 'bash {INSTALL_DIR}/{SCRIPT_NAME}' || /tmp/vp2-ssh.sh 1085 'bash {INSTALL_DIR}/{SCRIPT_NAME}'"
        if do_test
        else "true"
    )

    body = f"""set -euo pipefail
echo {pw_b64} | base64 -d > /tmp/vp8.pass
chmod 600 /tmp/vp8.pass
# reuse vp2-ssh helpers with custom password file if present; else sshpass-style via sshpass/plink on jump
# Prefer generic socks ssh wrapper if available
SOCKS=1084
echo {script_b64} | base64 -d > /tmp/{SCRIPT_NAME}
echo {install_b64} | base64 -d > /tmp/install-disk-hygiene.sh
chmod +x /tmp/{SCRIPT_NAME} /tmp/install-disk-hygiene.sh
export SSHPASS=$(cat /tmp/vp8.pass)
ssh_cmd() {{
  local port="$1"; shift
  sshpass -f /tmp/vp8.pass ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=25 \\
    -o ProxyCommand="nc -x 127.0.0.1:${{port}} %h %p" root@{host} "$@"
}}
scp_cmd() {{
  local port="$1"; shift
  sshpass -f /tmp/vp8.pass scp -o StrictHostKeyChecking=accept-new -o ConnectTimeout=25 \\
    -o ProxyCommand="nc -x 127.0.0.1:${{port}} %h %p" "$@"
}}
ok=0
for SOCKS in 1084 1085 1080; do
  if ssh_cmd $SOCKS "mkdir -p {INSTALL_DIR}"; then ok=1; break; fi
done
[[ $ok -eq 1 ]] || {{ echo FAIL_SSH; exit 1; }}
scp_cmd $SOCKS /tmp/{SCRIPT_NAME} root@{host}:{INSTALL_DIR}/{SCRIPT_NAME}
scp_cmd $SOCKS /tmp/install-disk-hygiene.sh root@{host}:/tmp/install-disk-hygiene.sh
ssh_cmd $SOCKS 'bash /tmp/install-disk-hygiene.sh; rm -f /tmp/install-disk-hygiene.sh'
{test_cmd} || true
rm -f /tmp/{SCRIPT_NAME} /tmp/install-disk-hygiene.sh /tmp/vp8.pass
echo JUMP_OK
"""
    rc, text = jump_run(body, timeout=360)
    ok = rc == 0 and "JUMP_OK" in text and "OK" in text
    return {
        "id": sid,
        "host": host,
        "ok": ok,
        "error": None if ok else f"jump deploy failed: {text[-500:]}",
        "cron_minute": minute,
        "test_out": text[-500:] if do_test else None,
    }


def deploy_direct(server: dict, env: dict, token: str, chat_id: str, do_test: bool) -> dict:
    if server.get("method") == "key":
        return deploy_openssh_key(server, env, token, chat_id, do_test)
    if server.get("id") == "vpn-vp8":
        # try direct first briefly handled by caller; this path is socks fallback only
        pass
    sid = server["id"]
    host = server["host"]
    label = f"{sid} {server.get('subdomain') or sid}"
    minute = cron_minute(sid)
    sb = ssh_base(server, env)
    pb = pscp_base(server, env)
    if not sb or not pb:
        return {"id": sid, "host": host, "ok": False, "error": "missing auth secret"}

    run(sb + [f"mkdir -p {INSTALL_DIR}"], timeout=60)
    rc, out, err = run(
        pb + [str(SCRIPT_SRC), f"root@{host}:{INSTALL_DIR}/{SCRIPT_NAME}"],
        timeout=90,
    )
    if rc != 0:
        return {"id": sid, "host": host, "ok": False, "error": f"pscp failed: {(err or out)[:200]}"}

    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8", newline="\n") as f:
        f.write(install_bash(token, chat_id, label, minute))
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

    result: dict = {"id": sid, "host": host, "ok": True, "error": None, "cron_minute": minute}
    if do_test:
        rc2, out2, err2 = run(sb + [f"{INSTALL_DIR}/{SCRIPT_NAME}"], timeout=180)
        result["test_ok"] = rc2 == 0
        result["test_out"] = ((out2 or "") + (err2 or ""))[-800:]
    return result


def deploy_vp2_via_jump(env: dict, token: str, chat_id: str, do_test: bool) -> dict:
    sid = "vpn-vp2"
    host = "150.241.86.99"
    label = f"{sid} vp2"
    minute = cron_minute(sid)
    pw = env.get("SSH_PASSWORD_VP2", "")
    if not pw:
        return {"id": sid, "host": host, "ok": False, "error": "missing SSH_PASSWORD_VP2"}

    script_b64 = base64.b64encode(SCRIPT_SRC.read_bytes()).decode()
    install_b64 = base64.b64encode(install_bash(token, chat_id, label, minute).encode()).decode()
    pw_b64 = base64.b64encode(pw.encode()).decode()
    test_cmd = f"/tmp/vp2-ssh.sh 1084 '{INSTALL_DIR}/{SCRIPT_NAME}'" if do_test else "true"

    body = f"""set -euo pipefail
echo {pw_b64} | base64 -d > /tmp/vp2.pass
chmod 600 /tmp/vp2.pass
echo {script_b64} | base64 -d > /tmp/{SCRIPT_NAME}
echo {install_b64} | base64 -d > /tmp/install-disk-hygiene.sh
chmod +x /tmp/{SCRIPT_NAME} /tmp/install-disk-hygiene.sh
/tmp/vp2-ssh.sh 1084 "mkdir -p {INSTALL_DIR}"
/tmp/vp2-scp.sh 1084 /tmp/{SCRIPT_NAME} root@{host}:{INSTALL_DIR}/{SCRIPT_NAME}
/tmp/vp2-scp.sh 1084 /tmp/install-disk-hygiene.sh root@{host}:/tmp/install-disk-hygiene.sh
/tmp/vp2-ssh.sh 1084 'bash /tmp/install-disk-hygiene.sh; rm -f /tmp/install-disk-hygiene.sh'
{test_cmd} || true
rm -f /tmp/{SCRIPT_NAME} /tmp/install-disk-hygiene.sh
echo JUMP_OK
"""
    rc, text = jump_run(body, timeout=300)
    ok = rc == 0 and "JUMP_OK" in text and "OK" in text
    return {
        "id": sid,
        "host": host,
        "ok": ok,
        "error": None if ok else f"jump deploy failed: {text[-400:]}",
        "cron_minute": minute,
        "test_out": text[-500:] if do_test else None,
    }


def main() -> int:
    if not SCRIPT_SRC.is_file():
        print(f"Missing {SCRIPT_SRC}", file=sys.stderr)
        return 2

    env = load_dotenv(ROOT / ".env")
    tg = load_tg_notify()
    token = str(tg.get("token", "")).strip()
    chat_id = str(tg.get("chat_id", "")).strip()
    if not token or not chat_id:
        print("Missing token/chat_id in telegram.local.json", file=sys.stderr)
        return 2

    servers = parse_inventory(ROOT / "inventory.yml")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    only = set(args)
    run_all_tests = "--test-all" in flags
    skip_test = "--no-test" in flags

    if only:
        servers = [s for s in servers if s["id"] in only or s.get("subdomain") in only]

    print(f"Servers: {len(servers)} chat_id={chat_id} (notify bot)", flush=True)
    results = []
    # one smoke test by default
    test_id = "vpn-vp12" if any(s["id"] == "vpn-vp12" for s in servers) else (servers[0]["id"] if servers else "")

    for i, s in enumerate(servers, 1):
        print(f"[{i}/{len(servers)}] {s['id']} {s['host']}:{s.get('ssh_port', 22)} ...", flush=True)
        do_test = (not skip_test) and (run_all_tests or s["id"] == test_id)
        if s["id"] == "vpn-vp2":
            r = deploy_vp2_via_jump(env, token, chat_id, do_test=do_test)
        elif s["id"] == "vpn-vp8":
            # direct often times out — SOCKS via jump
            r = deploy_vp8_via_jump(env, token, chat_id, do_test=do_test)
            if not r.get("ok"):
                # fallback direct once
                r2 = deploy_direct(s, env, token, chat_id, do_test=do_test)
                if r2.get("ok"):
                    r = r2
        else:
            r = deploy_direct(s, env, token, chat_id, do_test=do_test)
        status = "OK" if r["ok"] else "FAIL"
        extra = ""
        if r.get("test_ok") is True:
            extra = " test=OK"
        elif r.get("test_ok") is False:
            extra = " test=FAIL"
        elif do_test and s["id"] == "vpn-vp2" and r.get("ok"):
            extra = " test=ran"
        cm = r.get("cron_minute")
        cm_s = f"{cm:02d}" if isinstance(cm, int) else "?"
        print(
            f"  {status}{extra} cron=4:{cm_s}"
            + (f" {r['error']}" if r.get("error") else ""),
            flush=True,
        )
        if r.get("test_out") and do_test:
            safe = (r["test_out"][-400:]).encode("ascii", "replace").decode("ascii")
            print(f"  test_out: {safe}", flush=True)
        results.append(r)
        time.sleep(0.15)

    out_path = ROOT / "artifacts" / "disk-hygiene-deploy.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # strip secrets if any leaked into test_out
    safe_results = []
    for r in results:
        rr = dict(r)
        if "test_out" in rr and rr["test_out"]:
            rr["test_out"] = re.sub(r"\d{8,}:[A-Za-z0-9_-]+", "[token]", rr["test_out"])
        safe_results.append(rr)
    out_path.write_text(json.dumps(safe_results, indent=2, ensure_ascii=False), encoding="utf-8")
    ok = sum(1 for r in results if r["ok"])
    fail = len(results) - ok
    print(f"Done: {ok} ok, {fail} fail -> {out_path}", flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
