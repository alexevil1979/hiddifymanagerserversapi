#!/bin/bash
# Restore admin_users + users from a Hiddify backup JSON, then restore proxy paths only.
# Does NOT restore domains, protocols, private keys, or full hconfigs.
#
# Usage on the VPS (as root):
#   BACKUP_JSON=/tmp/restore-users.json \
#   ADMIN_PASS_FILE=/tmp/admin.pass \
#   bash scripts/restore-users-admins.sh
#
# BACKUP_JSON must contain at least admin_users and users.
# If it also has hconfigs (list or dict), these keys are restored when present:
#   proxy_path, proxy_path_admin, proxy_path_client
#
# After path restore this script runs apply_configs (needed for HAProxy routes).

set -euo pipefail
export NEEDRESTART_MODE=l

BACKUP_JSON="${BACKUP_JSON:-/tmp/restore-users.json}"
ADMIN_PASS_FILE="${ADMIN_PASS_FILE:-/tmp/admin.pass}"
SERVER_ID="${SERVER_ID:-panel}"

if [[ ! -f "$BACKUP_JSON" ]]; then
  echo "missing BACKUP_JSON=$BACKUP_JSON" >&2
  exit 1
fi

install -d -m 700 /var/backups/hiddify
cd /opt/hiddify-manager/hiddify-panel
out=$(/opt/hiddify-manager/.venv313/bin/hiddifypanel backup)
src=$(printf '%s\n' "$out" | awk '/^backup\/.*\.json$/ {print; exit}')
cp -a "/opt/hiddify-manager/hiddify-panel/$src" "/var/backups/hiddify/${SERVER_ID}-before-users-restore.json"
chmod 600 "/var/backups/hiddify/${SERVER_ID}-before-users-restore.json"
echo PRE_BACKUP_OK

/opt/hiddify-manager/.venv313/bin/python - <<PY
import json, os
from pathlib import Path

os.chdir("/opt/hiddify-manager/hiddify-panel")
for line in Path("app.cfg").read_text().splitlines():
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ[k.strip()] = v.strip().strip("'").strip('"')

from hiddifypanel.base import create_app
app = create_app(app_mode="cli")
data = json.loads(Path("$BACKUP_JSON").read_text(encoding="utf-8"))
payload = {
    "admin_users": data.get("admin_users") or [],
    "users": data.get("users") or [],
}
print("IN admins", len(payload["admin_users"]), "users", len(payload["users"]))

PATH_KEYS = ("proxy_path", "proxy_path_admin", "proxy_path_client")

def hconfigs_map(raw):
    if isinstance(raw, list):
        return {str(i.get("key")): i.get("value") for i in raw if isinstance(i, dict) and "key" in i}
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    return {}

wanted_paths = {k: v for k, v in hconfigs_map(data.get("hconfigs")).items() if k in PATH_KEYS and v}

with app.app_context():
    from hiddifypanel.database import db
    from hiddifypanel.models import User, AdminUser, StrConfig
    from hiddifypanel.panel import hiddify

    print("BEFORE users", User.query.count(), "admins", AdminUser.query.count())
    hiddify.set_db_from_json(
        payload,
        override_child_unique_id=False,
        set_users=True,
        set_domains=False,
        set_proxies=False,
        set_settings=False,
        remove_domains=False,
        remove_users=True,
        override_unique_id=False,
        set_admins=True,
        override_root_admin=False,
        replace_owner_admin=True,
        fix_admin_hierarchy=True,
        set_child=False,
    )
    print("AFTER users", User.query.count(), "admins", AdminUser.query.count())

    # Fresh install regenerates proxy paths. Restore only these three from backup.
    for key, value in wanted_paths.items():
        row = StrConfig.query.filter_by(key=key).first()
        if row is None:
            print("PATH_MISSING_KEY", key)
            continue
        old = row.value
        row.value = str(value)
        print("PATH_SET", key, "from", old, "to", row.value)
    if wanted_paths:
        db.session.commit()
        print("PATHS_RESTORED", len(wanted_paths))
    else:
        print("PATHS_SKIP no proxy_path* in backup hconfigs")

    owner = AdminUser.get_super_admin()
    card = {
        "admin_uuid": str(owner.uuid),
        "admins": AdminUser.query.count(),
        "users": User.query.count(),
        "proxy_path": getattr(StrConfig.query.filter_by(key="proxy_path").first(), "value", None),
        "proxy_path_admin": getattr(StrConfig.query.filter_by(key="proxy_path_admin").first(), "value", None),
        "proxy_path_client": getattr(StrConfig.query.filter_by(key="proxy_path_client").first(), "value", None),
    }
    Path("/tmp/restore-card.json").write_text(json.dumps(card), encoding="utf-8")
    print("CARD", json.dumps(card))
PY

if [[ -f "$ADMIN_PASS_FILE" ]]; then
  export HIDDIFY_ADMIN_PASSWORD="$(cat "$ADMIN_PASS_FILE")"
  rm -f "$ADMIN_PASS_FILE"
  /opt/hiddify-manager/.venv313/bin/python - <<'PY'
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
with app.app_context():
    from hiddifypanel.database import db
    from hiddifypanel.models import AdminUser
    AdminUser.get_super_admin().update_password(os.environ["HIDDIFY_ADMIN_PASSWORD"])
    db.session.commit()
    print("ADMIN_PASSWORD_RESET")
PY
fi

cd /opt/hiddify-manager
/opt/hiddify-manager/.venv313/bin/python common/commander.py apply-users
echo APPLY_USERS_OK

# proxy path changes need full apply for HAProxy/nginx routes
/opt/hiddify-manager/apply_configs.sh --no-gui --no-log
echo APPLY_CONFIGS_OK

cd /opt/hiddify-manager/hiddify-panel
out=$(/opt/hiddify-manager/.venv313/bin/hiddifypanel backup)
src=$(printf '%s\n' "$out" | awk '/^backup\/.*\.json$/ {print; exit}')
cp -a "/opt/hiddify-manager/hiddify-panel/$src" "/var/backups/hiddify/${SERVER_ID}-after-users-restore.json"
chmod 600 "/var/backups/hiddify/${SERVER_ID}-after-users-restore.json"
echo RESTORE_DONE
