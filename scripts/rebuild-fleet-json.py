#!/usr/bin/env python3
"""Rebuild docs/web/data/fleet.json from inventory + Cloudflare proxied fact."""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INV = ROOT / "inventory.yml"
OUT = ROOT / "docs" / "web" / "data" / "fleet.json"
ZONES = ["sdfsdfsdfsd.store", "losttv.site", "linkusers3.online"]


def load_dotenv() -> dict[str, str]:
    out: dict[str, str] = {}
    p = ROOT / ".env"
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
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
        g = lambda pat, default=None: (m.group(1) if (m := re.search(pat, p)) else default)
        modes = g(r"(?m)^\s+domain_modes:\s*(\[[^\]]*\])")
        # domains list
        dm = re.search(r"(?ms)^\s+domains:\n((?:\s+- .+\n)+)", p)
        domains = []
        if dm:
            domains = re.findall(r"(?m)^\s+- (.+)$", dm.group(1))
        servers.append(
            {
                "id": m_id.group(1),
                "host": m_host.group(1),
                "location": g(r"(?m)^\s+location:\s*(\S+)"),
                "group": g(r"(?m)^\s+group:\s*(\S+)"),
                "state": g(r"(?m)^\s+state:\s*(\S+)"),
                "ssh_port": int(g(r"(?m)^\s+ssh_port:\s*(\d+)", "22")),
                "ssh_user": g(r"(?m)^\s+ssh_user:\s*(\S+)", "root"),
                "auth_method": g(r"(?m)^\s+method:\s*(\S+)", "password"),
                "password_env": g(r"(?m)^\s+password_env:\s*(\S+)"),
                "key_path_env": g(r"(?m)^\s+key_path_env:\s*(\S+)"),
                "subdomain": g(r"(?m)^\s+subdomain:\s*(\S+)"),
                "domains": domains,
                "domain_modes": modes,
                "panel_domain": g(r'(?m)^\s+panel_domain:\s*"?([^"\n]+)"?'),
                "status_note": g(r'(?m)^\s+status_note:\s*"([^"]*)"'),
                "status_updated_at": g(r'(?m)^\s+status_updated_at:\s*"([^"]*)"'),
            }
        )
    return servers


def cf_api(token: str, path: str) -> dict:
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode())


def cf_proxied_map(token: str, servers: list[dict]) -> dict[str, dict]:
    """Return id -> {proxied: bool|None, detail: str}."""
    zone_ids: dict[str, str] = {}
    for z in ZONES:
        r = cf_api(token, f"/zones?name={urllib.parse.quote(z)}")
        results = r.get("result") or []
        if not results:
            continue
        zone_ids[z] = results[0]["id"]

    out: dict[str, dict] = {}
    for s in servers:
        sub = s.get("subdomain")
        if not sub or sub in ("null", "None"):
            out[s["id"]] = {"cf_proxied": None, "cf_detail": "no subdomain"}
            continue
        flags = []
        for z, zid in zone_ids.items():
            name = f"{sub}.{z}"
            q = urllib.parse.urlencode({"type": "A", "name": name})
            try:
                r = cf_api(token, f"/zones/{zid}/dns_records?{q}")
            except Exception as e:
                flags.append(f"{z}:err")
                continue
            recs = r.get("result") or []
            if not recs:
                flags.append(f"{z}:missing")
            else:
                flags.append(f"{z}:{'proxied' if recs[0].get('proxied') else 'dns'}")
        proxied_n = sum(1 for f in flags if f.endswith(":proxied"))
        dns_n = sum(1 for f in flags if f.endswith(":dns"))
        if proxied_n == len(flags) and proxied_n > 0:
            mode = True
        elif dns_n == len(flags) and dns_n > 0:
            mode = False
        elif proxied_n == 0 and dns_n == 0:
            mode = None
        else:
            mode = "mixed"
        out[s["id"]] = {"cf_proxied": mode, "cf_detail": ", ".join(flags)}
        print(s["id"], out[s["id"]], flush=True)
    return out


def normalize_mode(modes: str | None, cf) -> str:
    """CDN column: Cloudflare fact first; flag inventory mismatch."""
    m = (modes or "").lower()
    inv_cdn = "cdn" in m
    inv_direct = "direct" in m and "cdn" not in m
    if cf is True:
        if inv_direct:
            return "cdn≠inv"
        return "cdn"
    if cf is False:
        if inv_cdn:
            return "direct≠inv"
        return "direct"
    if cf == "mixed":
        return "mixed"
    if inv_cdn:
        return "cdn"
    if inv_direct:
        return "direct"
    return "—"


def main() -> int:
    env = load_dotenv()
    token = env.get("CLOUDFLARE_API_TOKEN", "")
    servers = parse_inventory(INV)
    cfmap = cf_proxied_map(token, servers) if token else {}

    for s in servers:
        cf = cfmap.get(s["id"], {})
        s["cf_proxied"] = cf.get("cf_proxied")
        s["cf_detail"] = cf.get("cf_detail")
        s["cdn"] = normalize_mode(s.get("domain_modes"), cf.get("cf_proxied"))

    payload = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M"),
        "servers": servers,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"WROTE {OUT} n={len(servers)}", flush=True)

    # print table for chat
    print("\nID\tCDN\tINV_MODES\tCF\tHOST\tSTATE")
    for s in sorted(servers, key=lambda x: x["id"]):
        print(
            f"{s['id']}\t{s['cdn']}\t{s.get('domain_modes') or '—'}\t{s.get('cf_detail') or '—'}\t{s['host']}\t{s.get('state')}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
