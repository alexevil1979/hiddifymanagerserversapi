#!/usr/bin/env bash
# Daily disk hygiene for Hiddify VPS: safe cleanup + Telegram status.
# Does NOT touch configs, SSL, Redis, panel DB, or active user data.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${DISK_HYGIENE_ENV:-$SCRIPT_DIR/.env}"
LOG_DIR="${DISK_HYGIENE_LOG_DIR:-/opt/hiddify-manager/log}"
LOG_FILE="${DISK_HYGIENE_LOG:-$LOG_DIR/disk-hygiene.log}"
STATE_DIR="/var/lib/disk-hygiene"
DRY_RUN="${DRY_RUN:-0}"

# Tunables (override via .env)
JOURNAL_MAX_SIZE="${JOURNAL_MAX_SIZE:-80M}"
JOURNAL_MAX_TIME="${JOURNAL_MAX_TIME:-7d}"
KEEP_PANEL_BACKUPS="${KEEP_PANEL_BACKUPS:-5}"
KEEP_VAR_BACKUPS="${KEEP_VAR_BACKUPS:-8}"
HIDDIFY_LOG_MAX_MB="${HIDDIFY_LOG_MAX_MB:-50}"
TMP_MAX_AGE_DAYS="${TMP_MAX_AGE_DAYS:-3}"
WARN_PCT="${WARN_PCT:-80}"
CRIT_PCT="${CRIT_PCT:-90}"
REPORT_MODE="${REPORT_MODE:-always}"   # always | warn | never

mkdir -p "$LOG_DIR" "$STATE_DIR"
chmod 700 "$STATE_DIR" 2>/dev/null || true

log() {
  echo "[$(date -Is)] $*" | tee -a "$LOG_FILE"
}

bytes_to_human() {
  awk -v b="${1:-0}" 'BEGIN {
    if (b < 1024) { printf "%dB", b; exit }
    if (b < 1048576) { printf "%.1fK", b/1024; exit }
    if (b < 1073741824) { printf "%.1fM", b/1048576; exit }
    printf "%.2fG", b/1073741824
  }'
}

disk_stats() {
  # prints: used_pct avail_human used_kb size_kb
  df -Pk / | awk 'NR==2 {gsub(/%/,"",$5); printf "%s %s %s %s", $5, $4, $3, $2}'
}

kb_free() {
  df -Pk / | awk 'NR==2 {print $4}'
}

send_telegram() {
  local text="$1"
  if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
    log "WARN: Telegram not configured, skip send"
    return 0
  fi
  local tmp
  tmp="$(mktemp)"
  # JSON-safe-ish: escape backslash and quotes
  local esc
  esc="$(printf '%s' "$text" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || printf '"%s"' "${text//\"/\\\"}")"
  printf '{"chat_id":"%s","text":%s}' "$TELEGRAM_CHAT_ID" "$esc" >"$tmp"
  if curl -sS --max-time 25 -X POST \
      -H 'Content-Type: application/json' \
      --data-binary @"$tmp" \
      "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      | grep -q '"ok":true'; then
    log "Telegram: sent"
  else
    log "WARN: Telegram send failed"
  fi
  rm -f "$tmp"
}

freed_kb_start=0
freed_note=()

note_free() {
  local label="$1"
  local before after delta
  before="$(kb_free)"
  shift
  if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY_RUN: $*"
    return 0
  fi
  # shellcheck disable=SC2068
  $@ >/dev/null 2>&1 || true
  after="$(kb_free)"
  delta=$((after - before))
  if [[ "$delta" -gt 0 ]]; then
    freed_note+=("${label}+$(bytes_to_human $((delta * 1024)))")
    log "cleanup $label: +${delta}KB free"
  fi
}

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

TELEGRAM_BOT_TOKEN="$(printf '%s' "${TELEGRAM_BOT_TOKEN:-}" | tr -d '[:space:]')"
TELEGRAM_CHAT_ID="$(printf '%s' "${TELEGRAM_CHAT_ID:-}" | tr -d '[:space:]')"
SERVER_LABEL="$(printf '%s' "${SERVER_LABEL:-$(hostname)}" | tr -d '\r' | sed "s/^'//;s/'$//")"

log "===== disk-hygiene start label=${SERVER_LABEL} dry=${DRY_RUN} ====="

read -r PCT_BEFORE AVAIL_BEFORE_KB _ _ <<<"$(disk_stats)"
AVAIL_BEFORE_H="$(bytes_to_human $((AVAIL_BEFORE_KB * 1024)))"
log "before: ${PCT_BEFORE}% used, avail ${AVAIL_BEFORE_H}"

# --- safe cleanups ---

# 1) systemd journal
note_free "journal" journalctl --vacuum-size="$JOURNAL_MAX_SIZE"
note_free "journal-time" journalctl --vacuum-time="$JOURNAL_MAX_TIME"

# 2) apt package cache
note_free "apt-clean" bash -c 'apt-get clean >/dev/null 2>&1 || true'
note_free "apt-lists-partial" bash -c 'rm -rf /var/cache/apt/archives/partial/* /var/cache/apt/archives/*.deb 2>/dev/null || true'

# 3) rotated / compressed system logs (keep current *.log)
note_free "varlog-rotated" bash -c '
  find /var/log -type f \( -name "*.gz" -o -name "*.xz" -o -name "*.old" -o -name "*.1" -o -name "*.2" -o -name "*.3" -o -name "*.4" -o -name "*.5" \) -delete 2>/dev/null || true
  find /var/log -type f -name "*.log.[0-9]*" -delete 2>/dev/null || true
'

# 4) truncate huge active logs (>100MB) without deleting the inode
note_free "truncate-huge-logs" bash -c '
  find /var/log -type f -name "*.log" -size +100M -exec truncate -s 0 {} \; 2>/dev/null || true
'

# 5) hiddify logs: keep under size budget (delete oldest *.log.* / large files)
note_free "hiddify-logs" bash -c '
  d=/opt/hiddify-manager/log
  [[ -d "$d" ]] || exit 0
  find "$d" -type f \( -name "*.gz" -o -name "*.1" -o -name "*.old" \) -mtime +2 -delete 2>/dev/null || true
  # if still large, truncate biggest active logs over 20M
  find "$d" -type f -size +20M -exec truncate -s 5M {} \; 2>/dev/null || true
  # cron/deploy logs older than 14d
  find "$d" -type f -name "*.cron.log" -mtime +14 -delete 2>/dev/null || true
  find "$d" -type f -name "disk-hygiene.log" -size +5M -exec truncate -s 1M {} \; 2>/dev/null || true
'

# 6) panel backup dir: keep newest N json/zip
note_free "panel-backups" bash -c '
  d=/opt/hiddify-manager/hiddify-panel/backup
  [[ -d "$d" ]] || exit 0
  keep='"$KEEP_PANEL_BACKUPS"'
  mapfile -t files < <(find "$d" -maxdepth 1 -type f \( -name "*.json" -o -name "*.zip" -o -name "*.tar.gz" -o -name "*.tgz" \) -printf "%T@ %p\n" 2>/dev/null | sort -nr | cut -d" " -f2-)
  n=${#files[@]}
  if (( n > keep )); then
    for ((i=keep; i<n; i++)); do rm -f -- "${files[$i]}"; done
  fi
'

# 7) /var/backups/hiddify: keep newest N (pre-* snapshots accumulate)
note_free "var-backups" bash -c '
  d=/var/backups/hiddify
  [[ -d "$d" ]] || exit 0
  keep='"$KEEP_VAR_BACKUPS"'
  mapfile -t files < <(find "$d" -maxdepth 1 -type f -printf "%T@ %p\n" 2>/dev/null | sort -nr | cut -d" " -f2-)
  n=${#files[@]}
  if (( n > keep )); then
    for ((i=keep; i<n; i++)); do
      base=$(basename "${files[$i]}")
      # never delete *configured* golden snapshots
      [[ "$base" == *configured* ]] && continue
      rm -f -- "${files[$i]}"
    done
  fi
'

# 8) tmp leftovers
note_free "tmp-old" bash -c '
  find /tmp /var/tmp -xdev -type f -mtime +'"$TMP_MAX_AGE_DAYS"' -not -path "*/systemd-private-*" -delete 2>/dev/null || true
'

# 9) core dumps (limited paths — full-disk find is too slow)
note_free "coredumps" bash -c 'rm -rf /var/lib/systemd/coredump/* /var/crash/* 2>/dev/null || true; find /tmp /var/tmp /opt /root -xdev -type f -name "core" -size +5M -mtime +1 -delete 2>/dev/null || true'

# 10) orphaned acme/http challenge junk (safe)
note_free "acme-tmp" bash -c 'rm -rf /tmp/acme* /tmp/hiddify-acme* 2>/dev/null || true'

read -r PCT_AFTER AVAIL_AFTER_KB _ _ <<<"$(disk_stats)"
AVAIL_AFTER_H="$(bytes_to_human $((AVAIL_AFTER_KB * 1024)))"
FREED_KB=$((AVAIL_AFTER_KB - AVAIL_BEFORE_KB))
if [[ "$FREED_KB" -lt 0 ]]; then FREED_KB=0; fi
FREED_H="$(bytes_to_human $((FREED_KB * 1024)))"

if (( PCT_AFTER >= CRIT_PCT )); then
  LEVEL="CRIT"
elif (( PCT_AFTER >= WARN_PCT )); then
  LEVEL="WARN"
else
  LEVEL="OK"
fi

DETAIL=""
if ((${#freed_note[@]} > 0)); then
  DETAIL=$(IFS=', '; echo "${freed_note[*]}")
fi

MSG="Disk ${SERVER_LABEL}
${LEVEL}: ${PCT_BEFORE}% → ${PCT_AFTER}% | free ${AVAIL_AFTER_H} | cleaned ${FREED_H}
$(hostname -I 2>/dev/null | awk '{print $1}') | $(date '+%Y-%m-%d %H:%M %Z')"
if [[ -n "$DETAIL" ]]; then
  MSG="${MSG}
${DETAIL}"
fi

printf '%s\n' "$MSG" >"$STATE_DIR/last-report.txt"
printf '{"label":"%s","level":"%s","pct_before":%s,"pct_after":%s,"avail_kb":%s,"freed_kb":%s,"ts":"%s"}\n' \
  "$SERVER_LABEL" "$LEVEL" "$PCT_BEFORE" "$PCT_AFTER" "$AVAIL_AFTER_KB" "$FREED_KB" "$(date -Is)" \
  >"$STATE_DIR/last.json"

log "after: ${PCT_AFTER}% used, avail ${AVAIL_AFTER_H}, cleaned ${FREED_H}, level=${LEVEL}"

should_report=0
case "$REPORT_MODE" in
  always) should_report=1 ;;
  warn) [[ "$LEVEL" != "OK" ]] && should_report=1 ;;
  never) should_report=0 ;;
  *) should_report=1 ;;
esac

if [[ "$should_report" == "1" && "$DRY_RUN" != "1" ]]; then
  send_telegram "$MSG"
fi

log "===== disk-hygiene done ====="
exit 0
