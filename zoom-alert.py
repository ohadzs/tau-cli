#!/usr/bin/env python3
"""zoom-alert.py — on lecture days Ohad attends by Zoom (Sun/Mon), DM him the join link.

Cron on zima1 fires at lecture time (see SCHEDULE); the link comes from
data/moodle-zoom.json, which moodle-sync.sh refreshes hourly 07-10.
A dated meeting for today wins; otherwise the course's recurring (no-start) meeting.
`--dry` prints instead of sending.
"""
import datetime, json, pathlib, sys, urllib.parse, urllib.request, zoneinfo

TZ = zoneinfo.ZoneInfo("Asia/Jerusalem")
HERE = pathlib.Path(__file__).resolve().parent
# Python weekday (Mon=0 .. Sun=6) -> [(course code, label, start)]
SCHEDULE = {
    6: [("0321-1838", "מבוא מתמטי לפיזיקה 1", "10:10")],
    0: [("0321-1100", "אלגברה לינארית", "09:10")],
}
CHAT = "6360233384"


def link_for(code, today, data):
    for c in data["courses"]:
        if c["code"] != code:
            continue
        ms = c["meetings"]
        for m in ms:
            if m["start"] and datetime.datetime.fromtimestamp(m["start"], TZ).date() == today:
                return m["join_url"]
        recurring = [m for m in ms if not m["start"] and code.replace("-", "") in (m.get("topic") or "")]
        recurring = recurring or [m for m in ms if not m["start"]]
        if recurring:
            return recurring[0]["join_url"]
    return None


def send(text):
    tok = (pathlib.Path.home() / ".telegram_bot_token").read_text().strip()
    body = urllib.parse.urlencode({"chat_id": CHAT, "text": text, "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", body, timeout=20).read()


def main():
    dry = "--dry" in sys.argv
    now = datetime.datetime.now(TZ)
    if "--day" in sys.argv:  # --day YYYY-MM-DD, for testing
        now = datetime.datetime.fromisoformat(sys.argv[sys.argv.index("--day") + 1]).replace(tzinfo=TZ)
    data = json.loads((HERE / "data/moodle-zoom.json").read_text())
    for code, label, start in SCHEDULE.get(now.weekday(), []):
        url = link_for(code, now.date(), data)
        text = f"🎥 {label} — {start}\n{url}" if url else f"⚠️ {label} — {start}: לא נמצא קישור זום ב-Moodle"
        print(now.strftime("%F %T"), code, url or "NO LINK")
        if not dry:
            send(text)


if __name__ == "__main__":
    main()
