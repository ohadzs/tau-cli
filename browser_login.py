"""Browser-driven access to the TAU portal.

Two things forced this design:

1. The portal doesn't authenticate anyone itself — it bounces to TAU's NetIQ SSO at
   `nidp.tau.ac.il`, whose form wants **three** fields: username, ID number, and
   password. (`ActionDoLogin` on the portal exists but is a dead end for SSO
   accounts: it answers "Invalid Login" whatever you send.)
2. The screenservice endpoints are not callable with empty inputs. Most need the
   student's program context threaded through `inputParameters`, and TAU's REST
   layer reports a missing context as — again — "Invalid Login". Even a `fetch`
   from inside the logged-in page fails without it.

So rather than reconstructing payloads, we drive the real screen and record what it
asks for. `screen()` navigates and returns every screenservice response the page
made: exactly the data the user sees, with no payload guesswork.
"""

import json

BASE = "https://my.tau.ac.il/TAU_Student/"
SSO_HOST = "nidp.tau.ac.il"

# The welcome interstitial sits in front of the login and swallows the redirect.
DISMISS_LABELS = ("Got it, don’t show again", "Got it, don't show again", "הבנתי")

SETTLE_MS = 9_000  # the SPA keeps a socket open, so networkidle never fires


class LoginFailed(Exception):
    pass


def _dismiss_intro(page):
    for label in DISMISS_LABELS:
        try:
            page.get_by_text(label, exact=False).first.click(timeout=4000)
            return True
        except Exception:
            continue
    return False


def _sign_in(page, username, id_number, password, timeout_ms):
    from playwright.sync_api import TimeoutError as PWTimeout

    page.goto(BASE, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(4000)
    _dismiss_intro(page)
    try:
        page.wait_for_selector("input[name=password]", timeout=timeout_ms)
    except PWTimeout:
        raise LoginFailed(f"never reached the SSO form; sat at {page.url}")

    page.fill("input[name=user_name]", username)
    page.fill("input[name=id_number]", id_number)
    page.fill("input[name=password]", password)
    page.press("input[name=password]", "Enter")
    page.wait_for_timeout(SETTLE_MS)

    if "nidp" in page.url or page.query_selector("input[name=password]"):
        raise LoginFailed(f"login didn't complete — still at {page.url}")


def screen(username, id_number, password, route,
           timeout_ms=60_000, headless=True):
    """Open one portal screen and return {endpoint: response-json} for it.

    `route` is a screen name from `tau map`, e.g. "Tuition" or "CourseGrades".
    """
    from playwright.sync_api import sync_playwright

    captured = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            page = browser.new_context(locale="he-IL").new_page()
            _sign_in(page, username, id_number, password, timeout_ms)

            def on_response(resp):
                if "/screenservices/" not in resp.url or resp.status != 200:
                    return
                name = resp.url.split("screenservices/TAU_Student/")[-1]
                try:
                    body = json.loads(resp.text())
                except Exception:
                    return
                data = body.get("data")
                if data:  # skip the version-only chatter
                    captured[name] = data

            page.on("response", on_response)
            page.goto(BASE + route, wait_until="domcontentloaded",
                      timeout=timeout_ms)
            page.wait_for_timeout(SETTLE_MS)
            return captured
        finally:
            browser.close()


def login_cookies(username, id_number, password, timeout_ms=60_000, headless=True):
    """Sign in and hand back the cookies (kept for the plain-HTTP client)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            ctx = browser.new_context(locale="he-IL")
            page = ctx.new_page()
            _sign_in(page, username, id_number, password, timeout_ms)
            return ctx.cookies()
        finally:
            browser.close()


def cookie_header(cookies):
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


# ------------------------------------------------------------ inquiry portal

PNIOT_LABEL = "מערכת פניות"      # the dashboard tile that opens the portal
PNIOT_MY_CASES = "הפניות שלי"
PNIOT_SETTLE_MS = 11_000          # FormTitan renders its tables late


def _click_leaf(frame, text):
    """Click the leaf element whose whole text is `text`. Returns True if found."""
    return frame.evaluate("""(t) => {
        const els = Array.from(document.querySelectorAll('*'))
            .filter(e => e.children.length === 0 && (e.innerText || '').trim() === t);
        if (!els.length) return false;
        els[0].click();
        return true;
    }""", text)


def _open_pniot(ctx, page, timeout_ms):
    """From a signed-in portal page, open the inquiry system and land on My Cases.

    The inquiry system is **not** OutSystems — it's a FormTitan app
    (`tau-int.formtitan.com/ftproject/crm_tau/`) opened in a second tab, and it's a
    SPA: navigating straight to /my_cases bounces back to Home, so the only way in
    is to click through.
    """
    page.get_by_text(PNIOT_LABEL, exact=False).first.click(timeout=timeout_ms)
    page.wait_for_timeout(PNIOT_SETTLE_MS)
    tabs = [pg for pg in ctx.pages if "formtitan" in pg.url]
    if not tabs:
        raise LoginFailed("the inquiry system never opened")
    ft = tabs[-1]
    ft.bring_to_front()
    if not _click_leaf(ft, PNIOT_MY_CASES):
        raise LoginFailed(f"no '{PNIOT_MY_CASES}' button on {ft.url}")
    ft.wait_for_timeout(PNIOT_SETTLE_MS)
    return ft


def _rows(ft):
    """The My-Cases table as [{number, subject, status}]."""
    raw = ft.evaluate("""() => Array.from(document.querySelectorAll('[role=row]'))
        .map(r => (r.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean))
        .filter(cells => cells.length >= 3 && /^\\d{6,}$/.test(cells[0]))""")
    return [{"number": c[0], "subject": c[1], "status": c[2]} for c in raw]


def new_inquiry(username, id_number, password, timeout_ms=60_000, wait=None):
    """Sign in and leave a *visible* browser sitting on the new-inquiry form.

    Filing is Ohad's to do: mail sent straight to a unit is rejected ("פנייתך
    נסגרה מכיוון שלא נפתחה דרך מערכת הפניות"), so the form is the only door, and
    the submit button stays his. `wait` is called once the form is up.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        try:
            ctx = browser.new_context(locale="he-IL", no_viewport=True)
            page = ctx.new_page()
            _sign_in(page, username, id_number, password, timeout_ms)
            page.get_by_text(PNIOT_LABEL, exact=False).first.click(timeout=timeout_ms)
            page.wait_for_timeout(PNIOT_SETTLE_MS)
            tabs = [pg for pg in ctx.pages if "formtitan" in pg.url]
            if not tabs:
                raise LoginFailed("the inquiry system never opened")
            ft = tabs[-1]
            ft.bring_to_front()
            if not _click_leaf(ft, "פניה חדשה"):
                raise LoginFailed(f"no 'פניה חדשה' button on {ft.url}")
            ft.wait_for_timeout(PNIOT_SETTLE_MS)
            (wait or (lambda: None))()
        finally:
            browser.close()


def inquiries(username, id_number, password, case=None,
              timeout_ms=60_000, headless=True):
    """List the inquiries, or open one and return it with its correspondence.

    Without `case`: [{number, subject, status}].
    With `case`: {number, subject, status, detail, emails} — `detail` is the case
    page (opening date, category, חוג, handler, the text he sent) and `emails` is
    whatever correspondence the portal keeps for it.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            ctx = browser.new_context(locale="he-IL")
            page = ctx.new_page()
            _sign_in(page, username, id_number, password, timeout_ms)
            ft = _open_pniot(ctx, page, timeout_ms)
            rows = _rows(ft)
            if case is None:
                return rows

            if not any(r["number"] == case for r in rows):
                raise LoginFailed(
                    f"no inquiry {case} — have: {', '.join(r['number'] for r in rows)}")
            row = next(r for r in rows if r["number"] == case)
            # a synthetic .click() on the cell does nothing here; the real mouse does
            ft.locator(f"text={case}").first.click(timeout=timeout_ms)
            ft.wait_for_timeout(PNIOT_SETTLE_MS)
            mails = [f.inner_text("body") for f in ft.frames if "emails" in f.url]
            return dict(row, detail=ft.main_frame.inner_text("body"), emails=mails)
        finally:
            browser.close()
