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
