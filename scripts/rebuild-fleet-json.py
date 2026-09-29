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
DOMAINS_YML = ROOT / "domains.yml"
FALLBACK_ZONES = [
    "sdfsdfsdfsd.store",
    "losttv.site",
    "linkusers3.online",
    "telegrambot.website",
    "teleworker.fun",
]


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


def load_zones() -> list[str]:
    if not DOMAINS_YML.is_file():
        return list(FALLBACK_ZONES)
    names = re.findall(r"(?m)^\s+- name:\s*(\S+)", DOMAINS_YML.read_text(encoding="utf-8", errors="replace"))
    return names or list(FALLBACK_ZONES)


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
        # domains list, plus dangling "- host" lines in the same card
        dm = re.search(r"(?ms)^\s+domains:\n((?:\s+- .+\n)+)", p)
        domains = []
        if dm:
            domains = re.findall(r"(?m)^\s+- (.+)$", dm.group(1))
        sub = g(r"(?m)^\s+subdomain:\s*(\S+)")
        zones = load_zones()
        seen = set(domains)
        for line in p.splitlines():
            mline = re.match(r"^\s+- (\S+)$", line)
            if not mline:
                continue
            host = mline.group(1)
            if any(host.endswith("." + z) for z in zones) and host not in seen:
                domains.append(host)
                seen.add(host)
        # ensure every inventory zone name is considered if the card uses this subdomain
        if sub and sub not in ("null", "None"):
            for z in zones:
                name = f"{sub}.{z}"
                if name not in seen:
                    # only add later if Cloudflare has the record
                    pass
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
                "status_note": g(r'(?m)^\s*status_note:\s*"([^"]*)"'),
                "status_updated_at": g(r'(?m)^\s*status_updated_at:\s*"([^"]*)"'),
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


def zone_of(domain: str, zones: list[str]) -> str | None:
    for z in sorted(zones, key=len, reverse=True):
        if domain == z or domain.endswith("." + z):
            return z
    return None


def cf_domain_modes(token: str, servers: list[dict], zones: list[str]) -> None:
    """Attach domain_rows [{domain, mode}] from Cloudflare A proxied flag."""
    zone_ids: dict[str, str] = {}
    for z in zones:
        r = cf_api(token, f"/zones?name={urllib.parse.quote(z)}")
        results = r.get("result") or []
        if results:
            zone_ids[z] = results[0]["id"]

    cache: dict[str, str] = {}

    def mode_of(domain: str) -> str:
        if domain in cache:
            return cache[domain]
        z = zone_of(domain, zones)
        zid = zone_ids.get(z or "")
        if not zid:
            cache[domain] = "unknown"
            return cache[domain]
        q = urllib.parse.urlencode({"type": "A", "name": domain})
        try:
            r = cf_api(token, f"/zones/{zid}/dns_records?{q}")
        except Exception:
            cache[domain] = "err"
            return cache[domain]
        recs = r.get("result") or []
        if not recs:
            cache[domain] = "missing"
        else:
            cache[domain] = "cdn" if recs[0].get("proxied") else "direct"
        return cache[domain]

    for s in servers:
        names = list(s.get("domains") or [])
        sub = s.get("subdomain")
        seen = set(names)
        if sub and sub not in ("null", "None"):
            for z in zones:
                name = f"{sub}.{z}"
                if name not in seen:
                    names.append(name)
                    seen.add(name)
        rows = []
        flags = []
        for name in names:
            mode = mode_of(name)
            if mode == "missing":
                continue
            rows.append({"domain": name, "mode": mode})
            flags.append(f"{name}:{mode}")
        s["domains"] = [r["domain"] for r in rows]
        s["domain_rows"] = rows
        proxied_n = sum(1 for r in rows if r["mode"] == "cdn")
        direct_n = sum(1 for r in rows if r["mode"] == "direct")
        if rows and proxied_n == len(rows):
            mode = True
        elif rows and direct_n == len(rows):
            mode = False
        elif proxied_n and direct_n:
            mode = "mixed"
        else:
            mode = None
        s["cf_proxied"] = mode
        s["cf_detail"] = ", ".join(flags)
        print(s["id"], s["cf_detail"], flush=True)


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
    zones = load_zones()
    servers = parse_inventory(INV)
    previous: dict[str, dict] = {}
    if OUT.is_file():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
            previous = {x["id"]: x for x in old.get("servers") or [] if x.get("id")}
        except Exception:
            previous = {}
    for s in servers:
        prev = previous.get(s["id"]) or {}
        if not s.get("status_note") and prev.get("status_note"):
            s["status_note"] = prev["status_note"]
            s["status_updated_at"] = prev.get("status_updated_at")
        for name in prev.get("domains") or []:
            if name not in s["domains"]:
                s["domains"].append(name)
    if token:
        cf_domain_modes(token, servers, zones)
    else:
        for s in servers:
            s["domain_rows"] = [{"domain": d, "mode": "unknown"} for d in s.get("domains") or []]
            s["cf_proxied"] = None
            s["cf_detail"] = "no token"

    for s in servers:
        s["cdn"] = normalize_mode(s.get("domain_modes"), s.get("cf_proxied"))

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
