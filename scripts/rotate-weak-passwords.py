#!/usr/bin/env python3
"""Rotate weak shared SSH passwords on park hosts. Updates local .env. No secrets in stdout summary."""
from __future__ import annotations

import base64
import json
import secrets
import string
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLINK = r"C:\Program Files\PuTTY\plink.exe"
ALPHABET = string.ascii_letters + string.digits

TARGETS = [
    ("vpn-vp1", "185.207.133.3", 22, "root", "SSH_PASSWORD_VP1", "direct"),
    ("vpn-vp2", "150.241.86.99", 22, "root", "SSH_PASSWORD_VP2", "socks"),
    ("vpn-vp3", "2.26.114.11", 22, "root", "SSH_PASSWORD_VP3", "direct"),
    ("vpn-vp7ru", "108.165.250.23", 22, "root", "SSH_PASSWORD_VP7RU", "direct"),
    ("vpn-vp9", "78.111.67.214", 22, "root", "SSH_PASSWORD_VP9", "direct"),
    ("vpn-vp12", "179.198.51.155", 22, "root", "SSH_PASSWORD_VP12", "direct"),
    ("vpn-vp13", "144.31.253.250", 22, "root", "SSH_PASSWORD_VP13", "direct"),
    ("vpn-vp14", "193.124.44.61", 22, "root", "SSH_PASSWORD_VP14", "direct"),
    ("vpn-vp16", "185.249.154.200", 22, "root", "SSH_PASSWORD_VP16", "direct"),
    ("vpn-vp18", "144.31.192.182", 22, "root", "SSH_PASSWORD_VP18", "direct"),
]


def gen_password() -> str:
    while True:
        p = "".join(secrets.choice(ALPHABET) for _ in range(24))
        if any(c.islower() for c in p) and any(c.isupper() for c in p) and any(c.isdigit() for c in p):
            return p


def load_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    env: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k] = v
    return lines, env


def save_env(path: Path, lines: list[str], env: dict[str, str]) -> None:
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if not line or line.startswith("#") or "=" not in line:
            out.append(line)
            continue
        k, _ = line.split("=", 1)
        if k in env:
            out.append(f"{k}={env[k]}")
            seen.add(k)
        else:
            out.append(line)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def plink_password(user: str, host: str, port: int, password: str, remote: str) -> str:
    args = [PLINK, "-ssh", f"{user}@{host}", "-P", str(port), "-pw", password, remote]
    p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(0.45)
    try:
        assert p.stdin is not None
        p.stdin.write(b"y\n")
        p.stdin.flush()
    except Exception:
        pass
    try:
        assert p.stdin is not None
        p.stdin.close()
    except Exception:
        pass
    try:
        out, err = p.communicate(timeout=50)
    except subprocess.TimeoutExpired:
        p.kill()
        return "TIMEOUT"
    return (out or b"").decode("utf-8", "replace") + "\n" + (err or b"").decode("utf-8", "replace")


def change_direct(host: str, port: int, user: str, old: str, new: str) -> tuple[bool, str]:
    b64 = base64.b64encode(new.encode()).decode()
    # remote bash expands base64 -d
    remote = f"bash -lc 'echo root:$(echo {b64} | base64 -d) | chpasswd && echo CHPASS_OK'"
    text = plink_password(user, host, port, old, remote)
    return ("CHPASS_OK" in text), text


def verify_direct(host: str, port: int, user: str, new: str) -> tuple[bool, str]:
    text = plink_password(user, host, port, new, "echo AUTH_OK; hostname")
    return ("AUTH_OK" in text), text


def run_jump_script(cfg: dict, body: str) -> str:
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh", encoding="utf-8", newline="\n") as f:
        f.write(body.replace("\r\n", "\n").replace("\r", "\n"))
        path = f.name
    try:
        args = [
            PLINK,
            "-ssh",
            "-batch",
            "-pw",
            cfg["ssh_password"],
            "-m",
            path,
            f"{cfg['ssh_user']}@{cfg['ssh_host']}",
        ]
        p = subprocess.run(args, capture_output=True, text=True, timeout=120)
        return (p.stdout or "") + "\n" + (p.stderr or "")
    finally:
        try:
            Path(path).unlink()
        except Exception:
            pass


def change_socks(cfg: dict, old: str, new: str) -> tuple[bool, str]:
    old_b64 = base64.b64encode(old.encode()).decode()
    new_b64 = base64.b64encode(new.encode()).decode()
    body = f"""set -e
echo {old_b64} | base64 -d > /tmp/vp2.pass
chmod 600 /tmp/vp2.pass
echo {new_b64} | base64 -d > /tmp/vp2.newpass
chmod 600 /tmp/vp2.newpass
cat > /tmp/vp2-chpass-inner.sh <<'INNER'
#!/bin/bash
set -e
NEW=$(cat /tmp/vp2.newpass)
echo "root:${{NEW}}" | chpasswd
echo CHPASS_OK
INNER
chmod +x /tmp/vp2-chpass-inner.sh
/tmp/vp2-scp.sh 1084 /tmp/vp2-chpass-inner.sh /tmp/vp2.newpass root@150.241.86.99:/tmp/
/tmp/vp2-ssh.sh 1084 'bash /tmp/vp2-chpass-inner.sh; rm -f /tmp/vp2.newpass /tmp/vp2-chpass-inner.sh'
echo {new_b64} | base64 -d > /tmp/vp2.pass
/tmp/vp2-ssh.sh 1084 'echo AUTH_OK; hostname'
rm -f /tmp/vp2.newpass /tmp/vp2-chpass-inner.sh
"""
    text = run_jump_script(cfg, body)
    ok = "CHPASS_OK" in text and "AUTH_OK" in text
    return ok, text


def main() -> None:
    env_path = ROOT / ".env"
    lines, env = load_env(env_path)
    cfg = json.loads((ROOT / "telegram.local.json").read_text(encoding="utf-8"))

    new_map = {penv: gen_password() for *_, penv, _ in TARGETS}
    assert len(set(new_map.values())) == len(new_map)

    results = []
    for sid, host, port, user, penv, mode in TARGETS:
        old = env.get(penv, "")
        new = new_map[penv]
        if not old:
            results.append({"id": sid, "host": host, "status": "no_old"})
            print(sid, "no_old")
            continue
        try:
            if mode == "direct":
                ok, text = change_direct(host, port, user, old, new)
                if not ok:
                    results.append({"id": sid, "host": host, "status": "chpass_fail", "detail": text[-220:]})
                    print(sid, "chpass_fail")
                    continue
                vok, vtext = verify_direct(host, port, user, new)
                if not vok:
                    results.append({"id": sid, "host": host, "status": "verify_fail", "detail": vtext[-220:]})
                    print(sid, "verify_fail")
                    continue
                env[penv] = new
                results.append({"id": sid, "host": host, "status": "ok", "mode": "direct"})
                print(sid, "ok")
            else:
                ok, text = change_socks(cfg, old, new)
                if ok:
                    env[penv] = new
                    results.append({"id": sid, "host": host, "status": "ok", "mode": "socks"})
                    print(sid, "ok socks")
                else:
                    results.append({"id": sid, "host": host, "status": "fail", "detail": text[-300:]})
                    print(sid, "fail socks")
        except Exception as e:
            results.append({"id": sid, "host": host, "status": "error", "detail": str(e)})
            print(sid, "error", e)

    save_env(env_path, lines, env)

    clean = []
    for r in results:
        item = {"id": r["id"], "host": r.get("host"), "status": r["status"], "mode": r.get("mode")}
        if r["status"] != "ok":
            item["detail"] = (r.get("detail") or "")[-180:]
        clean.append(item)

    out = ROOT / "artifacts" / "password-rotate-2026-09-28.json"
    out.write_text(
        json.dumps({"at": datetime.now().isoformat(timespec="minutes"), "results": clean}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    ok_n = sum(1 for r in results if r["status"] == "ok")
    print("DONE", ok_n, "/", len(results), "->", out)


if __name__ == "__main__":
    main()
