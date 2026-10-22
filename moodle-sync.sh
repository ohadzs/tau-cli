#!/usr/bin/env bash
# moodle-sync.sh — daily on zima1 (cron 06:00): every Moodle course's files -> Google Drive.
# moodle.py fetches only files not yet in $LOCAL; `rclone copy` then adds/updates in Drive
# and never deletes, so anything added there by hand (transcripts) stays.
# On failure: Telegram DM via ~/bin/tg-notify.
set -uo pipefail
cd "$(dirname "$0")"
LOCAL="${MOODLE_LOCAL:-$HOME/tau-cli/data/moodle}"
DEST="${MOODLE_DEST:-gdrive:אוניברסיטה}"
RCLONE="${RCLONE:-$HOME/bin/rclone}"

echo "== $(date '+%F %T')"
fail=""
python3 moodle.py sync "$LOCAL" || fail="moodle.py sync exited $?"
# Upload whatever did arrive, even after a partial failure.
out=$("$RCLONE" copy "$LOCAL" "$DEST" --stats-one-line -v 2>&1) \
  || fail="${fail:+$fail; }rclone copy failed"
grep -E "Copied|ERROR" <<<"$out" || true
if [ -n "$fail" ]; then
  echo "FAILED: $fail"
  ~/bin/tg-notify "Moodle → Drive failed" "$fail — see ~/tau-cli/data/moodle-sync.log on zima1"
fi
