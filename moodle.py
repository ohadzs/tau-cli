"""TAU Moodle: list enrolled courses and pull a course's files.

Moodle sits behind the same NetIQ SSO as the portal, but on a different form contract:
fields are `#Ecom_User_ID` (username), `#Ecom_User_Pid` (ID number), `#Ecom_Password`.
Credentials come from the same place as `tau` (Keychain `tau` + ~/.config/tau/{user,id}).

    .venv/bin/python moodle.py courses
    .venv/bin/python moodle.py pull <course-id> <dir> [--videos]

`pull` keeps Moodle's section layout (one folder per section). Lecture videos run
~0.3–0.7 GB each, so they're skipped unless --videos.
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
    """-> [(section, pluginfile-url)] for every file resource on the course page."""
    page.goto(COURSE_URL.format(course_id), wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    items = page.evaluate("""() => [...document.querySelectorAll('li.activity')].map(a => {
        const l = a.querySelector('a[href*="/mod/resource/"]');
        const s = a.closest('li.section,[data-for=section]');
        return {href: l ? l.href : '',
                sec: s ? ((s.querySelector('.sectionname,h3') || {}).innerText || '').trim() : ''};
      }).filter(x => x.href)""")
    out, seen = [], set()
    for it in items:
        if it["href"] in seen:
            continue
        seen.add(it["href"])
        r = ctx.request.get(it["href"], max_redirects=0)
        url = r.headers.get("location")
        if not url:  # embedded resource: the file link sits in the page
            m = re.search(r'https://moodle\.tau\.ac\.il/pluginfile\.php/[^"\'<> ]+', r.text())
            if not m:
                continue
            url = m.group(0).replace("&amp;", "&")
        out.append(((it["sec"] or "כללי").replace("/", "-"), url))
    return out


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("courses")
    p = sub.add_parser("pull")
    p.add_argument("course_id")
    p.add_argument("dir")
    p.add_argument("--videos", action="store_true")
    args = ap.parse_args()

    tau = _tau()
    user, pw = tau.credentials()
    idn = tau.read_id_number()
    if not (user and pw and idn):
        sys.exit("missing credentials — see README")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pwr:
        browser = pwr.chromium.launch(headless=True, channel="chrome")
        try:
            ctx = browser.new_context(locale="he-IL")
            page = ctx.new_page()
            sign_in(page, user, idn, pw)
            if args.cmd == "courses":
                for c in courses(page):
                    print(f'{c["id"]}\t{c["fullname"]}\t{c.get("coursecategory", "")}')
                return
            files = course_files(ctx, page, args.course_id)
            cookie = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies())
        finally:
            browser.close()

    # Download outside the browser: curl streams big files instead of buffering them.
    root = pathlib.Path(args.dir)
    for sec, url in files:
        name = urllib.parse.unquote(url.split("?")[0].rsplit("/", 1)[-1])
        if name.lower().endswith(VIDEO_EXT) and not args.videos:
            print(f"skip video\t{sec}/{name}")
            continue
        dest = root / sec / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        ok = subprocess.run(["curl", "-sfL", "-H", f"Cookie: {cookie}", "-o", str(dest), url]).returncode == 0
        print(f"{'ok' if ok else 'FAIL'}\t{sec}/{name}")


if __name__ == "__main__":
    main()
