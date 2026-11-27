"""TAU Moodle: list enrolled courses and pull a course's files.

Moodle sits behind the same NetIQ SSO as the portal, but on a different form contract:
fields are `#Ecom_User_ID` (username), `#Ecom_User_Pid` (ID number), `#Ecom_Password`.
Credentials come from the same place as `tau` (Keychain `tau` + ~/.config/tau/{user,id}).

    .venv/bin/python moodle.py courses
    .venv/bin/python moodle.py pull <course-id> <dir> [--videos]
    .venv/bin/python moodle.py sync <dir> [--videos]
    .venv/bin/python moodle.py grades [course]
    .venv/bin/python moodle.py recordings [course]
    .venv/bin/python moodle.py links [course]
    .venv/bin/python moodle.py panopto-cookies <file>

`sync` pulls every enrolled course into <dir>/<course name>/ and only fetches files that aren't
there yet, or that the lecturer replaced under the same name (the old copy goes to
<dir>/../moodle-replaced/) — the zima1 cron runs it, then rclone copies <dir> to Drive. In the same session it
writes <dir>/../moodle-deadlines.json (every assignment due date + whether it's submitted, its sheet, and
'no_box': homework sheets posted before any submission box exists) and appends
each newly fetched file to <dir>/../moodle-new.jsonl — the study page's sync (ohadzvishaboo
sync/study_sync.py) reads both. It also writes <dir>/../moodle-zoom.json: every meeting in each course's
Zoom LTI activity (start, join URL with passcode) — ohad-mcp's zoom_link reads it — and
<dir>/../moodle-links.json: every non-file link per course (mod/url resolved to its target, links in labels and
section summaries; {name, url, kind, section, cmid}) — ohad-mcp's learning tracks read it. Quiz pages are never opened.
Files Ohad deleted on purpose (e.g. a superseded homework sheet) are listed in <dir>/../moodle-removed.txt,
one path per line relative to <dir> — sync never fetches them again.
A file the lecturer took off Moodle leaves the mirror after 3 runs in a row missing (see prune()): kept in
<dir>/../moodle-replaced/, listed in <dir>/../moodle-pruned.txt for moodle-sync.sh to trash in Drive.
`pull` keeps Moodle's section layout (one folder per section, a subfolder per Moodle folder). Lecture videos run
~0.3–0.7 GB each, so they're skipped unless --videos.
`recordings` reads the Panopto block on each course page live (lecture + recitation recordings, title + viewer link);
[course] = part of the course name or number. `sync` also writes the same list for every course to
<dir>/../moodle-recordings.json ({at, courses: [{id, code, dir, recordings: [{title, url}]}]}) — ohad-mcp's
transcribe tool picks "the last lecture in X" from it.
`sync` also writes <dir>/../moodle-grades.json: per course its user grade report (every item: grade, max, percent,
weight, the report's feedback) + for each assignment the feedback box on its read-only view.php (graded at/by, the
grader's comment, feedback files). The grader's files (annotated PDF) download into <assignment folder>/משוב/ and so
reach Drive; prune() never touches them. Quiz pages are never opened — quiz grades come from the report.
`grades [course]` prints the same live.
`panopto-cookies <file>` signs in to Moodle, then to TAU's Panopto through its Moodle provider, and writes the
Panopto cookies as a Netscape cookies file (mode 600) — yt-dlp / `transcribe` take it to download a recording.
"""

import argparse
import importlib.machinery
import importlib.util
import json
import os
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


# A homework sheet posted as a file: "תרגיל בית 2", "שיעורי בית 3" — never "פתרון …" or "תרגול …".
SHEET = re.compile(r"^(?:תרגיל(?:\s+בית)?|שיעורי\s+בית|גיליון)\s*(\d+)\s*$")


def file_name(url):
    return urllib.parse.unquote(url.split("?")[0].rsplit("/", 1)[-1]).strip()


def course_files(ctx, page, course_id, sheets=None, assign_dirs=None):
    """-> [(section path, pluginfile-url)] for every file, folder and assignment attachment on the course page.
    sheets: a list to append this course's homework sheets to ({number, name, url, path, box}, path relative
    to the course folder; box = the sheet is attached to the submission box itself).
    assign_dirs: a dict to fill with {assignment cmid: its folder, relative to the course folder} — grades()
    puts the grader's feedback files in <that folder>/משוב/. Feedback files are left to grades(): the
    annotated PDF has the same name as his submission and would land on top of it."""
    page.goto(COURSE_URL.format(course_id), wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    items = page.evaluate("""() => [...document.querySelectorAll('li.activity')].map(a => {
        const s = a.closest('li.section,[data-for=section]');
        const l = a.querySelector('a[href*="/mod/resource/"], a[href*="/mod/folder/"], a[href*="/mod/assign/"]');
        return {href: l ? l.href : '',
                // an assignment's attachments sit on its page, like a folder's files
                folder: a.classList.contains('modtype_folder') || a.classList.contains('modtype_assign'),
                assign: a.classList.contains('modtype_assign'),
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
            links = [u for u in links if "/assignfeedback_" not in u]
            out += [(f"{sec}/{name}", u) for u in dict.fromkeys(links)]
            if it["assign"] and it["href"] and assign_dirs is not None:
                cm = urllib.parse.parse_qs(urllib.parse.urlsplit(it["href"]).query).get("id", [""])[0]
                if cm.isdigit():
                    assign_dirs[int(cm)] = f"{sec}/{name}"
            num = re.search(r"\d+", name)
            mine = [u for u in links if "assignsubmission" not in u]   # not his own hand-in
            if it["assign"] and num and mine and sheets is not None:
                sheets.append({"number": int(num.group()), "name": name, "url": it["href"], "box": True,
                               "path": f"{sec}/{name}/{file_name(mine[0])}"})
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
        m = SHEET.match(it["name"].strip())
        if m and sheets is not None:
            sheets.append({"number": int(m.group(1)), "name": it["name"].strip(), "url": it["href"], "box": False,
                           "path": f"{sec}/{file_name(url)}"})
    return out


def course_links(ctx, page, course_id):
    """Every link the lecturer posted that isn't a file: URL activities (mod/url, resolved to where they point),
    links inside text-and-media labels, and links in section summaries. Read-only: one GET per mod/url, without
    following the redirect; quiz pages are never opened (or listed). Expects the course page already loaded
    (course_files leaves it there).
    -> [{name, url, kind: url|label|section, section, cmid}]"""
    if f"id={course_id}" not in page.url:
        page.goto(COURSE_URL.format(course_id), wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
    items = page.evaluate("""() => {
        const secName = el => { const s = el.closest('li.section,[data-for=section]');
            return s ? ((s.querySelector('.sectionname,h3') || {}).innerText || '').trim() : ''; };
        const out = [];
        document.querySelectorAll('li.activity.modtype_url').forEach(a => {
            const l = a.querySelector('a[href*="/mod/url/view.php"]');
            if (l) out.push({kind: 'url', href: l.href, sec: secName(a),
                name: ((a.querySelector('.instancename,.activityname') || {}).innerText || l.innerText || '').trim().split('\\n')[0]});
        });
        const ext = (sel, kind) => document.querySelectorAll(sel).forEach(a => out.push({kind, href: a.href, sec: secName(a),
            name: (a.innerText || a.title || '').trim().replace(/\\s+/g, ' ')}));
        ext('li.activity.modtype_label a[href], li.activity.label a[href]', 'label');
        ext('li.section .summary a[href], [data-for=section] .summarytext a[href], [data-for=sectioninfo] a[href]', 'section');
        return out;
    }""")
    out, seen = [], set()
    for it in items:
        href = it["href"]
        if not href.startswith("http") or "/mod/quiz/" in href or href in seen:
            continue
        seen.add(href)
        row = {"name": it["name"], "url": href, "kind": it["kind"], "section": it["sec"] or "כללי", "cmid": None}
        if it["kind"] == "url":
            row["cmid"] = int(urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get("id", ["0"])[0]) or None
            try:
                r = ctx.request.get(href, max_redirects=0)
                target = r.headers.get("location")
                if not target:   # "click to open" page: the link sits in .urlworkaround
                    m = re.search(r'class="urlworkaround"[^>]*>.*?href="([^"]+)"', r.text(), re.S)
                    target = m.group(1).replace("&amp;", "&") if m else ""
                if target and "/mod/quiz/" not in target:
                    row["url"] = target
            except Exception as e:
                print(f"link FAIL\t{course_id}\t{href}\t{e}")
        elif "moodle.tau.ac.il" in href and "/pluginfile.php/" not in href:
            continue   # in-Moodle navigation inside a label (another activity, the forum) — not enrichment
        out.append(row)
    return out


def match_sheets(assignments, sheets):
    """Pair each submission box with its sheet (same course + number): sets a["sheet"] = {number, path, url}.
    -> the sheets no box was opened for yet, which Moodle's calendar can't show."""
    by_key = {}
    for s in sorted(sheets, key=lambda s: s["box"]):   # a sheet file on the page wins over a box attachment
        by_key.setdefault((s["course"], s["number"]), s)
    boxed = set()
    for a in assignments:
        num = re.search(r"\d+", a["name"])
        if not num:
            continue
        key = (a["course"], int(num.group()))
        boxed.add(key)
        if key in by_key:
            s = by_key[key]
            a["sheet"] = {"number": s["number"], "path": s["path"], "url": s["url"]}
    return [s for k, s in by_key.items() if k not in boxed]


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


GRADE_REPORT = "https://moodle.tau.ac.il/grade/report/user/index.php?id={}"
FEEDBACK_DIR = "משוב"   # the grader's files sit in <assignment folder>/משוב/ — prune() never touches them
HE_MONTHS = {m: i + 1 for i, m in enumerate(["ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני", "יולי", "אוגוסט",
                                             "ספטמבר", "אוקטובר", "נובמבר", "דצמבר"])}


def _num(s):
    try:
        return float(s.replace(",", ""))
    except (AttributeError, ValueError):
        return None


def _he_date(s):
    """'יום רביעי, 11 נובמבר 2026, 17:00' -> '2026-11-11 17:00' (Israel time, as Moodle shows it); else as is."""
    m = re.search(r"(\d{1,2})\s+(\S+)\s+(\d{4}),\s*(\d{1,2}:\d{2})", s or "")
    if not m or m.group(2) not in HE_MONTHS:
        return s or ""
    return f"{m.group(3)}-{HE_MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d} {int(m.group(4)[:-3]):02d}{m.group(4)[-3:]}"


def overview_totals(page):
    """{course id: course total as the overview report shows it ('87.65', '-')} — it can show a total whose items
    the course hides from the user report."""
    page.goto("https://moodle.tau.ac.il/grade/report/overview/index.php", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    rows = page.evaluate("""() => [...document.querySelectorAll('table tr')].map(tr => {
        const a = tr.querySelector('a[href*="id="]');
        return a && tr.cells.length > 1 ? [new URL(a.href).searchParams.get('id'), tr.cells[1].textContent.trim()] : null;
      }).filter(Boolean)""")
    return {int(i): g for i, g in rows if i and i.isdigit()}


def assignment_feedback(page, url):
    """The 'משוב' box on a read-only mod/assign/view.php: grade, when, by whom, the comment (full text, not the
    folded summary) and the grader's files (annotated PDF / feedback files). Parsed with DOMParser — no scripts run."""
    html = page.context.request.get(url).text()
    return page.evaluate("""(html) => {
      const d = new DOMParser().parseFromString(html, 'text/html');
      const box = d.querySelector('.feedback');
      if (!box) return null;
      const out = {grade: '', graded_at: '', graded_by: '', comments: '', files: []};
      box.querySelectorAll('tr').forEach(tr => {
        const th = tr.querySelector('th'), td = tr.querySelector('td');
        if (!th || !td) return;
        const label = th.textContent.trim();
        if (td.querySelector('[class*="assignfeedback_comments"]')) {
          const el = td.querySelector('[class*="full_assignfeedback_comments"]') ||
                     td.querySelector('[class*="summary_assignfeedback_comments"]');
          // keep the grader's line breaks: <br>, </p>, </li> -> newline, then plain text
          const t = d.createElement('div');
          t.innerHTML = el.innerHTML.replace(/<br\\s*\\/?>|<\\/p>|<\\/li>/gi, '\\n');
          out.comments = t.textContent.split('\\n').map(s => s.trim()).filter(Boolean).join('\\n');
        } else if (/^ציון|^Grade/.test(label)) out.grade = td.textContent.replace(/\\s+/g, ' ').trim();
        else if (/נבדק ב|Graded on/.test(label)) out.graded_at = td.textContent.trim();
        else if (/נבדק על|Graded by/.test(label))
          out.graded_by = [...td.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim();
      });
      out.files = [...new Set([...box.querySelectorAll('a[href*="/assignfeedback_"]')].map(a => a.href))];
      return out;
    }""", html)


def grades(page, course, assign_dirs):
    """Read-only: the course's user grade report (every grade item: grade, range, weight, the report's feedback
    column) + for each assignment the feedback box on its own page. Quiz items come from the report only —
    quiz pages are never opened.
    -> ({id, code, dir, total, items: [...]}, [(section path, feedback-file url)]) — the second goes to download()
    with the course's other files; paths in items are relative to the moodle/ folder."""
    page.goto(GRADE_REPORT.format(course["id"]), wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    rows = page.evaluate("""() => [...document.querySelectorAll('table.user-grade tr')].map(tr => {
        const th = tr.querySelector('th.column-itemname');
        if (!th || th.classList.contains('category')) return null;
        const cols = {};
        tr.querySelectorAll('td[class*="column-"]').forEach(td => {
          const c = [...td.classList].find(k => k.startsWith('column-'));
          cols[c.slice(7)] = td.textContent.replace(/\\s+/g, ' ').trim();
        });
        const a = th.querySelector('a[href]');
        return {name: (th.querySelector('.gradeitemheader,.rowtitle') || th).textContent.trim(),
                type: (th.querySelector('span.small') || {}).textContent || '',
                total: th.classList.contains('baggt') || !!th.querySelector('.courseitem'),
                url: a ? a.href : '', cols};
      }).filter(Boolean)""")
    cdir = course_dir_name(course)
    items, files, total = [], [], None
    for r in rows:
        c = r["cols"]
        if not c:   # the header row
            continue
        grade = c.get("grade", "")
        lo, _, hi = c.get("range", "").replace("–", "-").partition("-")
        mx = _num(hi)
        if r["total"]:
            if total is None:   # the first aggregate row is the course total (category subtotals come after)
                total = {"grade": grade, "max": mx, "percent": c.get("percentage")}
            continue
        g = _num(grade)
        pct = c.get("percentage") or (f"{round(100 * g / mx, 1)} %" if g is not None and mx else None)
        cm = urllib.parse.parse_qs(urllib.parse.urlsplit(r["url"]).query).get("id", [""])[0]
        it = {"name": r["name"], "type": r["type"].strip(), "url": r["url"],
              "cmid": int(cm) if cm.isdigit() else None, "grade": grade if grade not in ("", "-") else None,
              "max": mx, "percent": pct, "weight": c.get("weight"),
              "feedback": c.get("feedback") or "", "graded_at": "", "graded_by": "", "comments": "", "files": []}
        if "/mod/assign/" in r["url"]:
            try:
                fb = assignment_feedback(page, r["url"])
            except Exception as e:
                print(f"feedback FAIL\t{course['id']}\t{r['name']}\t{e}")
                fb = None
            if fb:
                it.update(graded_at=_he_date(fb["graded_at"]), graded_by=fb["graded_by"], comments=fb["comments"])
                folder = assign_dirs.get(it["cmid"]) or f"ציונים ומשובים/{r['name'].replace('/', '-')}"
                for u in fb["files"]:
                    files.append((f"{folder}/{FEEDBACK_DIR}", u))
                    it["files"].append(f"{cdir}/{folder}/{FEEDBACK_DIR}/{file_name(u)}")
        items.append(it)
    return {"id": course["id"], "code": course_code(course), "dir": cdir, "total": total, "items": items}, files


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


PANOPTO = "https://tau.cloud.panopto.eu"


def panopto_cookies(ctx, page, out):
    """Panopto has its own login page with a provider list ("Academic Year 2026/2027" = Moodle2026); picking the
    current Moodle provider bounces through the already signed-in Moodle and back. Writes the tau.cloud.panopto.eu
    cookies (.ASPXAUTH is the one that matters) as a Netscape cookies file."""
    provider = "Moodle" + re.search(r"/(\d{4})/", SSO_START).group(1)
    page.goto(f"{PANOPTO}/Panopto/Pages/Auth/Login.aspx?ReturnUrl=%2fPanopto%2fPages%2fSessions%2fList.aspx",
              wait_until="domcontentloaded", timeout=60_000)
    page.select_option("#providerDropdown", provider)
    page.click("#PageContentPlaceholder_loginControl_externalLoginButton")
    page.wait_for_url("**/Sessions/List.aspx**", timeout=60_000)
    cks = [c for c in ctx.cookies() if "panopto" in c["domain"]]
    if not any(c["name"] == ".ASPXAUTH" for c in cks):
        sys.exit("Panopto sign-in did not produce .ASPXAUTH")
    lines = ["# Netscape HTTP Cookie File"]
    for c in cks:
        d = c["domain"]
        lines.append("\t".join([d, "TRUE" if d.startswith(".") else "FALSE", c["path"],
                                "TRUE" if c["secure"] else "FALSE", str(int(c["expires"]) if c["expires"] > 0 else 0),
                                c["name"], c["value"]]))
    out = pathlib.Path(out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.touch(mode=0o600)
    out.chmod(0o600)
    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {len(cks)} Panopto cookies to {out}")


def course_dir_name(course):
    """'0321110001 - אלגברה לינארית לפיזיקה' -> 'אלגברה לינארית לפיזיקה' (the Drive folder name)."""
    return re.sub(r"^\d+\s*-\s*", "", course["fullname"]).replace("/", "-").strip()


def remote_changed(dest, url, cookie):
    """Did the lecturer replace this file under the same name? Moodle's date is newer than our copy, or the
    size differs. Anything unclear (request failed, header missing) counts as not changed."""
    import email.utils
    r = subprocess.run(["curl", "-sfIL", "--max-time", "20", "-H", f"Cookie: {cookie}", url],
                       capture_output=True, text=True)
    if r.returncode:
        return False
    headers = {}
    for line in r.stdout.splitlines():  # with redirects, the last response wins
        k, _, v = line.partition(":")
        if v:
            headers[k.strip().lower()] = v.strip()
    st = dest.stat()
    size = headers.get("content-length")
    if size and size.isdigit() and int(size) != st.st_size:
        return True
    try:
        return email.utils.parsedate_to_datetime(headers["last-modified"]).timestamp() > st.st_mtime
    except (KeyError, TypeError, ValueError):
        return False


def replace_file(dest, url, cookie, base):
    """Fetch the new version beside the tree, swap it in only once it's whole, and keep the old one in
    <base>/moodle-replaced/<date>/ (outside what goes to Drive). -> 'ok', 'same' or 'FAIL'."""
    import filecmp, shutil, time
    tmp_dir = base / "moodle-tmp"
    tmp_dir.mkdir(exist_ok=True)
    tmp = tmp_dir / dest.name
    if subprocess.run(["curl", "-sfL", "-H", f"Cookie: {cookie}", "-o", str(tmp), url]).returncode \
            or not tmp.exists() or not tmp.stat().st_size:
        tmp.unlink(missing_ok=True)
        return "FAIL"
    if filecmp.cmp(tmp, dest, shallow=False):
        tmp.unlink()
        os.utime(dest)  # same bytes: don't ask again
        return "same"
    keep = base / "moodle-replaced" / time.strftime("%Y-%m-%d_%H%M") / dest.relative_to(base)
    keep.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dest, keep)
    os.replace(tmp, dest)
    return "ok"


def download(files, root, cookie, videos=False, skip_existing=False, log=None, removed=(), check=False, base=None):
    """Download outside the browser: curl streams big files instead of buffering them. -> #failed
    log: a .jsonl path — each new file fetched is appended there ({at, path, url}, path relative to its parent).
    A replaced file isn't logged as new: its Drive file keeps its id, and new_moodle_files shows it by date.
    check: only print which files were replaced on Moodle."""
    import time
    failed = 0
    for sec, url in files:
        name = file_name(url)
        if name.lower().endswith(VIDEO_EXT) and not videos:
            if not skip_existing:  # the daily sync would repeat this every morning
                print(f"skip video\t{sec}/{name}")
            continue
        dest = root / sec / name
        if dest in removed:
            continue
        if skip_existing and dest.exists() and dest.stat().st_size:
            if not remote_changed(dest, url, cookie):
                continue
            if check:
                print(f"would replace\t{sec}/{name}")
                continue
            result = replace_file(dest, url, cookie, base)
            failed += result == "FAIL"
            print(f"replaced {result}\t{sec}/{name}")
            continue
        if check:
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


PRUNE_AFTER = 3        # hourly runs in a row a file must be missing from Moodle before it goes
PRUNE_MAX_SHARE = 0.5  # more than this share of a course missing at once = a page that didn't load; touch nothing


def prune(todo, root, base, check=False):
    """Files the lecturer took off Moodle (e.g. a homework sheet re-posted under a new name) leave the mirror too.
    A local file under a current course folder that Moodle no longer lists for PRUNE_AFTER runs in a row is moved
    to <base>/moodle-replaced/<date>/ and its path (relative to root) appended to <base>/moodle-pruned.txt;
    moodle-sync.sh then deletes it in Drive (Drive's trash, 30 days). Transcripts (made by hand) are never touched.
    check: only print what's missing; state is left alone."""
    import shutil, time
    state_f = base / "moodle-missing.json"
    try:
        state = json.loads(state_f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    new_state, gone = {}, []
    for sub_dir, files in todo:
        course = root / sub_dir
        if not files or not course.is_dir():
            continue  # an empty list is a page that didn't load, not a course emptied out
        listed = {course / sec / file_name(url) for sec, url in files}
        local = [p for p in course.rglob("*") if p.is_file() and "תמלול" not in p.name
                 and FEEDBACK_DIR not in p.relative_to(course).parts]   # grader feedback stays
        missing = [p for p in local if p not in listed]
        if len(missing) > PRUNE_MAX_SHARE * max(len(local), 1):
            print(f"prune skip\t{sub_dir}: {len(missing)}/{len(local)} files missing — looks like a bad page load")
            continue
        for p in missing:
            rel = str(p.relative_to(root))
            n = state.get(rel, 0) + 1
            if check:
                print(f"missing on Moodle ({n}/{PRUNE_AFTER})\t{rel}")
                continue
            if n < PRUNE_AFTER:
                new_state[rel] = n
                continue
            keep = base / "moodle-replaced" / time.strftime("%Y-%m-%d_%H%M") / root.name / rel
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), keep)
            gone.append(rel)
            print(f"gone from Moodle\t{rel}")
    if check:
        return
    state_f.write_text(json.dumps(new_state, ensure_ascii=False, indent=1), encoding="utf-8")
    if gone:
        with open(base / "moodle-pruned.txt", "a", encoding="utf-8") as f:
            f.write("".join(r + "\n" for r in gone))


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
    p.add_argument("--check", action="store_true", help="only list files replaced on Moodle; change nothing")
    p = sub.add_parser("links")
    p.add_argument("course", nargs="?", default="")
    p = sub.add_parser("grades")
    p.add_argument("course", nargs="?", default="")
    p = sub.add_parser("recordings")
    p.add_argument("course", nargs="?", default="")
    p = sub.add_parser("panopto-cookies")
    p.add_argument("file")
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
            if args.cmd == "links":
                for c in courses(page):
                    if args.course in c["fullname"]:
                        for l in course_links(ctx, page, c["id"]):
                            print(f'{course_dir_name(c)}\t{l["section"]}\t{l["kind"]}\t{l["name"]}\t{l["url"]}')
                return
            if args.cmd == "grades":
                for c in courses(page):
                    if args.course in c["fullname"]:
                        dirs = {}
                        course_files(ctx, page, c["id"], None, dirs)
                        g, _ = grades(page, c, dirs)
                        print(json.dumps(g, ensure_ascii=False, indent=1))
                return
            if args.cmd == "recordings":
                for c in courses(page):
                    if args.course in c["fullname"]:
                        for title, url in recordings(page, c["id"]):
                            print(f"{course_dir_name(c)}\t{title}\t{url}")
                return
            if args.cmd == "panopto-cookies":
                panopto_cookies(ctx, page, args.file)
                return
            if args.cmd == "sync":
                cs = courses(page)
                todo, sheets, links, marks = [], [], [], []
                for c in cs:
                    found, dirs = [], {}
                    files = course_files(ctx, page, c["id"], found, dirs)
                    todo.append((course_dir_name(c), files))
                    try:  # links are a side-trip too: never cost the file sync
                        links.append({"id": c["id"], "code": course_code(c), "dir": course_dir_name(c),
                                      "links": course_links(ctx, page, c["id"])})
                    except Exception as e:
                        print(f"links FAIL\t{c['id']}\t{e}")
                    try:  # grades + the grader's feedback: same rule, and its files download with the course's
                        g, fb_files = grades(page, c, dirs)
                        marks.append(g)
                        files += [f for f in fb_files if f not in files]
                    except Exception as e:
                        print(f"grades FAIL\t{c['id']}\t{e}")
                    sheets += [{**s, "course": c["id"], "path": f"{course_dir_name(c)}/{s['path']}"} for s in found]
                try:  # never let the deadlines side-trip cost the file sync
                    assignments = deadlines(page)
                    meta = {"at": int(__import__("time").time()),
                            "courses": [{"id": c["id"], "code": course_code(c), "dir": course_dir_name(c)} for c in cs],
                            "no_box": match_sheets(assignments, sheets),
                            "assignments": assignments}
                    out = pathlib.Path(args.dir).parent / "moodle-deadlines.json"
                    out.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
                except Exception as e:
                    print(f"deadlines FAIL\t{e}")
                if marks:
                    try:
                        tot = overview_totals(page)
                    except Exception as e:
                        print(f"grades overview FAIL\t{e}")
                        tot = {}
                    for g in marks:
                        g["overview_total"] = tot.get(g["id"])
                    # a course with no report and no total (exam archive, a training) isn't graded
                    marks = [g for g in marks if g["items"] or g["total"] or g["overview_total"] not in (None, "-", "")]
                    out = pathlib.Path(args.dir).parent / "moodle-grades.json"
                    out.write_text(json.dumps({"at": int(__import__("time").time()), "courses": marks},
                                              ensure_ascii=False, indent=1), encoding="utf-8")
                if links:
                    out = pathlib.Path(args.dir).parent / "moodle-links.json"
                    out.write_text(json.dumps({"at": int(__import__("time").time()), "courses": links},
                                              ensure_ascii=False, indent=1), encoding="utf-8")
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
                recs = []
                for c in cs:  # Panopto block, per course; a block that won't load costs only its own course
                    try:
                        recs.append({"id": c["id"], "code": course_code(c), "dir": course_dir_name(c),
                                     "recordings": [{"title": t, "url": u} for t, u in recordings(page, c["id"])]})
                    except Exception as e:
                        print(f"recordings FAIL\t{c['id']}\t{e}")
                if recs:
                    out = pathlib.Path(args.dir).parent / "moodle-recordings.json"
                    out.write_text(json.dumps({"at": int(__import__("time").time()), "courses": recs},
                                              ensure_ascii=False, indent=1), encoding="utf-8")
            else:
                todo = [("", course_files(ctx, page, args.course_id))]
            cookie = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies())
        finally:
            browser.close()

    root = pathlib.Path(args.dir)
    log = root.parent / "moodle-new.jsonl" if args.cmd == "sync" else None
    rm_list = root.parent / "moodle-removed.txt"
    removed = {root / l.strip() for l in rm_list.read_text(encoding="utf-8").splitlines() if l.strip()} \
        if args.cmd == "sync" and rm_list.exists() else set()
    failed = sum(download(files, root / sub_dir, cookie, args.videos, skip_existing=args.cmd == "sync", log=log,
                          removed=removed, check=getattr(args, "check", False), base=root.parent)
                 for sub_dir, files in todo)
    if args.cmd == "sync":
        try:  # cleanup never costs the sync
            prune(todo, root, root.parent, check=args.check)
        except Exception as e:
            print(f"prune FAIL\t{e}")
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
