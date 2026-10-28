"""TAU Moodle: list enrolled courses and pull a course's files.

Moodle sits behind the same NetIQ SSO as the portal, but on a different form contract:
fields are `#Ecom_User_ID` (username), `#Ecom_User_Pid` (ID number), `#Ecom_Password`.
Credentials come from the same place as `tau` (Keychain `tau` + ~/.config/tau/{user,id}).

    .venv/bin/python moodle.py courses
    .venv/bin/python moodle.py pull <course-id> <dir> [--videos]
    .venv/bin/python moodle.py sync <dir> [--videos]
    .venv/bin/python moodle.py recordings [course]

`sync` pulls every enrolled course into <dir>/<course name>/ and only fetches files that aren't
there yet — the daily zima1 cron runs it, then rclone copies <dir> to Drive. In the same session it
writes <dir>/../moodle-deadlines.json (every assignment due date + whether it's submitted) and appends
each newly fetched file to <dir>/../moodle-new.jsonl — the study page's sync (ohadzvishaboo
sync/study_sync.py) reads both. It also writes <dir>/../moodle-zoom.json: every meeting in each course's
Zoom LTI activity (start, join URL with passcode) — ohad-mcp's zoom_link reads it.
`pull` keeps Moodle's section layout (one folder per section, a subfolder per Moodle folder). Lecture videos run
~0.3–0.7 GB each, so they're skipped unless --videos.
`recordings` reads the Panopto block on each course page live (lecture + recitation recordings, title + viewer link);
[course] = part of the course name or number.
"""

import argparse
import importlib.machinery
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
SSO_START = "https://moodle.tau.ac.il/2026/auth/saml2/login.php"
COURSE_URL = "https://moodle.tau.ac.il/course/view.php?id={}"
VIDEO_EXT = (".mp4", ".mov", ".m4v", ".webm")
# Set by a failed login, so a daily cron can't keep hammering a wrong password into a lockout.
# Delete it once the password is fixed.
LOGIN_BLOCK = pathlib.Path("~/.config/tau/login-blocked").expanduser()


def _tau():
    loader = importlib.machinery.SourceFileLoader("taucli", str(HERE / "tau"))
    spec = importlib.util.spec_from_loader("taucli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def sign_in(page, user, id_number, password):
    page.goto(SSO_START, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector("#Ecom_Password", timeout=60_000)
    page.fill("#Ecom_User_ID", user)
    page.fill("#Ecom_User_Pid", id_number)
    page.fill("#Ecom_Password", password)
    page.press("#Ecom_Password", "Enter")
    page.wait_for_timeout(10_000)
    if "nidp" in page.url:
        LOGIN_BLOCK.touch()
        sys.exit("Moodle login failed (wrong credentials?) — stop before retrying: lockout risk")


def courses(page):
    sesskey = page.evaluate("() => M.cfg.sesskey")
    res = page.evaluate("""async (sk) => {
      const r = await fetch(M.cfg.wwwroot + '/lib/ajax/service.php?sesskey=' + sk, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify([{index: 0,
          methodname: 'core_course_get_enrolled_courses_by_timeline_classification',
          args: {offset: 0, limit: 0, classification: 'all', sort: 'fullname'}}])});
      return await r.json();
    }""", sesskey)[0]
    if res.get("error"):
        sys.exit(json.dumps(res, ensure_ascii=False))
    return res["data"]["courses"]


def course_files(ctx, page, course_id):
    """-> [(section path, pluginfile-url)] for every file, folder and assignment attachment on the course page."""
    page.goto(COURSE_URL.format(course_id), wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    items = page.evaluate("""() => [...document.querySelectorAll('li.activity')].map(a => {
        const s = a.closest('li.section,[data-for=section]');
        const l = a.querySelector('a[href*="/mod/resource/"], a[href*="/mod/folder/"], a[href*="/mod/assign/"]');
        return {href: l ? l.href : '',
                // an assignment's attachments sit on its page, like a folder's files
                folder: a.classList.contains('modtype_folder') || a.classList.contains('modtype_assign'),
                // inline folders have no .instancename: take the first plain text line
                name: ((a.querySelector('.instancename,.activityname') || {}).innerText ||
                       a.innerText.split('\\n').map(t => t.trim()).find(t => t &&
                         !/סימון כבוצע|הורדת התיקייה|Mark as done|Download folder/.test(t) &&
                         !/\\.[a-z0-9]{2,4}$/i.test(t)) || '').trim().split('\\n')[0],
                inline: [...a.querySelectorAll('a[href*="pluginfile.php"]')].map(x => x.href),
                sec: s ? ((s.querySelector('.sectionname,h3') || {}).innerText || '').trim() : ''};
      })""")
    out, seen = [], set()
    for it in items:
        sec = (it["sec"] or "כללי").replace("/", "-")
        if it["folder"]:
            # A folder shown inline already lists its files; otherwise open its page.
            links = it["inline"]
            if not links and it["href"]:
                html = ctx.request.get(it["href"]).text()
                links = [m.replace("&amp;", "&") for m in
                         re.findall(r'https://moodle\.tau\.ac\.il/pluginfile\.php/[^"\'<> ]+', html)]
            name = re.sub(r"\s+", " ", it["name"]).replace("/", "-") or "תיקייה"
            out += [(f"{sec}/{name}", u) for u in dict.fromkeys(links)]
            continue
        if not it["href"] or it["href"] in seen:
            continue
        seen.add(it["href"])
        r = ctx.request.get(it["href"], max_redirects=0)
        url = r.headers.get("location")
        if not url:  # embedded resource: the file link sits in the page
            m = re.search(r'https://moodle\.tau\.ac\.il/pluginfile\.php/[^"\'<> ]+', r.text())
            if not m:
                continue
            url = m.group(0).replace("&amp;", "&")
        out.append((sec, url))
    return out


def call(page, method, args):
    """One Moodle ajax web-service call (the ones the site's own JS uses) -> its data."""
    res = page.evaluate("""async ([m, a]) => {
      const r = await fetch(M.cfg.wwwroot + '/lib/ajax/service.php?sesskey=' + M.cfg.sesskey, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify([{index: 0, methodname: m, args: a}])});
      return await r.json();
    }""", [method, args])[0]
    if res.get("error"):
        raise RuntimeError(json.dumps(res, ensure_ascii=False)[:300])
    return res["data"]


def course_code(course):
    """'0321110001 - …' -> '0321-1100' (the TAU course number, as in the timetable and the calendar)."""
    d = re.match(r"\d{8}", course["fullname"])
    return f"{d.group(0)[:4]}-{d.group(0)[4:]}" if d else ""


def deadlines(page):
    """Every assignment due date from ~4 months back to ~5 ahead, from Moodle's calendar (read-only).
    An assignment still waiting for a submission carries an 'action'; a submitted one doesn't — but neither
    does one not yet open to him, so a missing action is checked on the assignment page."""
    import datetime
    today = datetime.date.today()   # ~a semester back and forward; months outside it don't change any more
    first, last = (today - datetime.timedelta(days=120)).replace(day=1), today + datetime.timedelta(days=150)
    out, y, m = {}, first.year, first.month
    while (y, m) <= (last.year, last.month):
        for w in call(page, "core_calendar_get_calendar_monthly_view", {"year": y, "month": m})["weeks"]:
            for d in w["days"]:
                for e in d["events"]:
                    if e.get("modulename") != "assign" or e.get("eventtype") != "due":
                        continue
                    out[e["id"]] = {"id": e["id"], "instance": e["instance"], "name": e.get("activityname") or e["name"],
                                    "due": e["timestart"], "url": e.get("url", ""), "course": e["course"]["id"],
                                    "submitted": not e.get("action")}
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    # No action also means "not open to you yet" (hidden / restricted) — confirm on the assignment's own page
    for a in out.values():
        if a["submitted"] and a["url"]:
            a["submitted"] = submitted(page, a["url"])
    return sorted(out.values(), key=lambda a: a["due"])


def submitted(page, url):
    """Read-only look at mod/assign/view.php: Moodle marks a handed-in attempt with the
    'submissionstatussubmitted' class on the status cell (any language)."""
    r = page.context.request.get(url)
    return r.ok and "submissionstatussubmitted" in r.text()


def zoom(page, course_id):
    """Every Zoom meeting listed in the course's Zoom LTI activity (the "ZOOM" link on the course page).
    The LTI page (applications.zoom.us/lti/rich) fetches its Upcoming list as JSON from its own API, so
    catch that response and page through the rest the same way. occurrenceId is the start in epoch ms
    (startTime is UTC); recurring meetings with no fixed time have neither. joinUrl carries the passcode
    (pwd=), so it joins directly."""
    import datetime
    page.goto(COURSE_URL.format(course_id), wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    ltis = page.evaluate("""() => [...new Set([...document.querySelectorAll('a[href*="/mod/lti/view.php"]')]
        .filter(a => /zoom/i.test(a.innerText)).map(a => new URL(a.href).searchParams.get('id')))]""")
    out = []
    for lti in ltis:
        with page.expect_response(lambda r: "/lti/rich/meeting/upComing/" in r.url, timeout=60_000) as resp:
            page.goto(f"https://moodle.tau.ac.il/mod/lti/launch.php?id={lti}", wait_until="domcontentloaded")
        first = resp.value
        res = first.json()["result"]
        rows = list(res["list"])
        for n in range(2, -(-res["total"] // res["pageSize"]) + 1):
            url = re.sub(r"([?&])page=\d+", rf"\g<1>page={n}", first.url)
            # replay the page's own request (its headers carry Zoom's anti-CSRF token)
            try:
                more = page.context.request.get(url, headers={k: v for k, v in first.request.all_headers().items()
                                                              if not k.startswith(":")})
                body = more.json()
                rows += body["result"]["list"]
            except Exception as e:  # keep the pages already in hand
                print(f"zoom page {n} FAIL\t{course_id}\t{e}")
                break
        for m in rows:
            start = None
            if m.get("occurrenceId"):
                start = int(m["occurrenceId"]) // 1000
            elif m.get("startTime"):
                start = int(datetime.datetime.fromisoformat(m["startTime"]).replace(tzinfo=datetime.timezone.utc).timestamp())
            out.append({"lti": int(lti), "topic": m.get("topic", ""), "number": m.get("meetingNumber", ""),
                        "start": start, "duration": m.get("duration", 0), "join_url": m.get("joinUrl", "")})
    return out


def recordings(page, course_id):
    """Every recording in the course page's Panopto block ("הקלטות הקורס בשרת הוידאו Panopto"), as
    (title, viewer URL). The block loads by AJAX after the page, and keeps all sessions in the DOM even
    the ones folded under "תצוגה מלאה"."""
    page.goto(COURSE_URL.format(course_id), wait_until="domcontentloaded")
    try:
        page.wait_for_selector('a[href*="panopto"][href*="Viewer.aspx"]', timeout=20_000)
    except Exception:
        return []
    return page.evaluate("""() => [...new Map([...document.querySelectorAll('a[href*="Viewer.aspx"]')]
        .map(a => [a.href, a.innerText.trim()])).entries()].map(([u, t]) => [t, u])""")


def course_dir_name(course):
    """'0321110001 - אלגברה לינארית לפיזיקה' -> 'אלגברה לינארית לפיזיקה' (the Drive folder name)."""
    return re.sub(r"^\d+\s*-\s*", "", course["fullname"]).replace("/", "-").strip()


def download(files, root, cookie, videos=False, skip_existing=False, log=None):
    """Download outside the browser: curl streams big files instead of buffering them. -> #failed
    log: a .jsonl path — each file fetched is appended there ({at, path, url}, path relative to its parent)."""
    import time
    failed = 0
    for sec, url in files:
        name = urllib.parse.unquote(url.split("?")[0].rsplit("/", 1)[-1]).strip()
        if name.lower().endswith(VIDEO_EXT) and not videos:
            if not skip_existing:  # the daily sync would repeat this every morning
                print(f"skip video\t{sec}/{name}")
            continue
        dest = root / sec / name
        if skip_existing and dest.exists() and dest.stat().st_size:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        ok = subprocess.run(["curl", "-sfL", "-H", f"Cookie: {cookie}", "-o", str(dest), url]).returncode == 0
        failed += not ok
        print(f"{'ok' if ok else 'FAIL'}\t{sec}/{name}")
        if ok and log:
            with open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M"), "path": str(dest.relative_to(root.parent)),
                                    "url": url}, ensure_ascii=False) + "\n")
    return failed


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("courses")
    p = sub.add_parser("pull")
    p.add_argument("course_id")
    p.add_argument("dir")
    p.add_argument("--videos", action="store_true")
    p = sub.add_parser("sync")
    p.add_argument("dir")
    p.add_argument("--videos", action="store_true")
    p = sub.add_parser("recordings")
    p.add_argument("course", nargs="?", default="")
    args = ap.parse_args()

    if LOGIN_BLOCK.exists():
        sys.exit(f"last Moodle login failed — fix the password, then rm {LOGIN_BLOCK}")
    tau = _tau()
    user, pw = tau.credentials()
    idn = tau.read_id_number()
    if not (user and pw and idn):
        sys.exit("missing credentials — see README")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pwr:
        # System Chrome on the Mac; playwright's own Chromium on Linux (zima1 has no Chrome).
        browser = pwr.chromium.launch(headless=True, channel="chrome" if sys.platform == "darwin" else None)
        try:
            ctx = browser.new_context(locale="he-IL")
            page = ctx.new_page()
            sign_in(page, user, idn, pw)
            if args.cmd == "courses":
                for c in courses(page):
                    print(f'{c["id"]}\t{c["fullname"]}\t{c.get("coursecategory", "")}')
                return
            if args.cmd == "recordings":
                for c in courses(page):
                    if args.course in c["fullname"]:
                        for title, url in recordings(page, c["id"]):
                            print(f"{course_dir_name(c)}\t{title}\t{url}")
                return
            if args.cmd == "sync":
                cs = courses(page)
                todo = [(course_dir_name(c), course_files(ctx, page, c["id"])) for c in cs]
                try:  # never let the deadlines side-trip cost the file sync
                    meta = {"at": int(__import__("time").time()),
                            "courses": [{"id": c["id"], "code": course_code(c), "dir": course_dir_name(c)} for c in cs],
                            "assignments": deadlines(page)}
                    out = pathlib.Path(args.dir).parent / "moodle-deadlines.json"
                    out.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
                except Exception as e:
                    print(f"deadlines FAIL\t{e}")
                zm = []
                for c in cs:  # same: a Zoom page that won't load costs only its own course
                    try:
                        zm.append({"id": c["id"], "code": course_code(c), "dir": course_dir_name(c),
                                   "meetings": zoom(page, c["id"])})
                    except Exception as e:
                        print(f"zoom FAIL\t{c['id']}\t{e}")
                if zm:
                    out = pathlib.Path(args.dir).parent / "moodle-zoom.json"
                    out.write_text(json.dumps({"at": int(__import__("time").time()), "courses": zm},
                                              ensure_ascii=False, indent=1), encoding="utf-8")
            else:
                todo = [("", course_files(ctx, page, args.course_id))]
            cookie = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies())
        finally:
            browser.close()

    root = pathlib.Path(args.dir)
    log = root.parent / "moodle-new.jsonl" if args.cmd == "sync" else None
    failed = sum(download(files, root / sub_dir, cookie, args.videos, skip_existing=args.cmd == "sync", log=log)
                 for sub_dir, files in todo)
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
