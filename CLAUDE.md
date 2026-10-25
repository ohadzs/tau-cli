# tau-cli — developer context

A standing connector for Tel Aviv University's student portal, reverse-engineered because the data
behind it is unreachable from outside a login. Personal data (ID, program, tuition figures) stays
out of this repo.

- **What it is:** `tau` = one Python file, I/O-free core (`Session` + `Api`) + argparse CLI.
  stdlib only. An MCP adapter could sit on the same core; not built (no need yet).
- **Run:** `./tau <map|login|probe|call>`. See README.
- **Secrets (macOS Keychain):** service `tau` (acct = ID number, pw = portal password, seeded once
  by the user) → session cached in service `tau-session`. Nothing on disk, nothing in argv.
- **The portal is OutSystems Reactive.** Endpoints are `POST /TAU_Student/screenservices/
  TAU_Student/<Flow>/<Screen>/<Action>` with `{versionInfo:{moduleVersion, apiVersion},
  viewName, inputParameters}`. Both version keys change on every redeploy, so they are
  **discovered at runtime**, never hardcoded: `moduleVersion` from `moduleservices/moduleinfo`,
  `apiVersion` from the screen's `*.mvc.js` (see `discover()`).
- **Why `map` needs no login:** the client bundle is public. This is the cheap way to find where a
  piece of data lives before spending a session on it.

## Gotchas

- **CSRF:** the login response returns `X-CSRFToken`; every later call must send it back or the
  server 403s. `Session` absorbs it automatically — don't bypass `Session.request`.
- **Session expiry is silent** — calls come back as an OutSystems `exception` object rather than an
  HTTP error. `Api.logged_in()` is the cheap probe; `open_session()` re-authenticates on it.
- **`probe` calls every endpoint of a screen with empty inputs.** Fine for the read-only data
  actions these screens expose — but read the endpoint names before probing an unfamiliar screen,
  since the same mechanism would happily fire a mutating action.
- **Bidding is a separate app** (`ims.tau.ac.il/Bidd/`, legacy ASP + JS frameset, its own login).
  Not covered. It's only live during a registration round, so reverse-engineer it during one.

## The inquiry system is a second app

`tau pniot` covers TAU's **מערכת פניות** — a **FormTitan** app (`tau-int.formtitan.com/
ftproject/crm_tau/`), not OutSystems, reached by clicking the dashboard tile (it opens a
second tab). It's a SPA: `goto('/my_cases')` bounces to Home, so the flow is click
"הפניות שלי" → click the case number. Clicking a Power-Table row needs a **real mouse**
(`locator.click()`); a synthetic `element.click()` from page JS silently does nothing.
`pniot new` opens the form **headful** and stops — filing is Ohad's action, not the tool's.

⚠️ Mail sent straight to a unit is auto-rejected ("לא נפתחה דרך מערכת הפניות"), and answers
land in the TAU mailbox rather than Gmail — the portal is the only reliable read path.

## Screens worth knowing

`Tuition`, `CourseGrades`, `WeeklySchedule`, `ExamsAndTasks`, `AcademicDashboard`,
`StutiesPrograms`, `RequestsAndApprovals`, `StudentCard`. `tau map` lists all 60-odd.

## Moodle is a third app

`moodle.py` (`courses`, `pull <course-id> <dir> [--videos]`) covers `moodle.tau.ac.il`. Same
NetIQ SSO, different form: `#Ecom_User_ID` (username, **not** the ID number) /
`#Ecom_User_Pid` / `#Ecom_Password`. Course list = Moodle's own ajax
`core_course_get_enrolled_courses_by_timeline_classification` with `M.cfg.sesskey`; files =
resolve each `/mod/resource/` link to its `pluginfile.php` URL, then curl with the session
cookies. Lecture videos are 0.3–0.7 GB each, so `pull` skips them by default.

The username lives in `~/.config/tau/user`; `credentials()` reads the Keychain `tau` item for
that account, because an older `tau` item keyed by ID number (stale password) can coexist.
Playwright drives the system Chrome (`channel="chrome"`), not its own bundled Chromium.

**Daily sync on zima1** (clone `/home/ohad/tau-cli`): cron 06:00 runs `moodle-sync.sh` =
`moodle.py sync data/moodle` (every course, new files only, no videos) + `rclone copy` to Drive
`אוניברסיטה/` (never deletes). Off macOS the password comes from `~/.config/tau/password` (600).
A failed login drops `~/.config/tau/login-blocked` and every later run refuses to log in until
it's removed — the lockout guard. Failure → Telegram DM. The tool only ever opens
`/mod/resource/` and `/mod/folder/` links — never quiz/assign pages; keep it that way.
In the same login `sync` also writes `data/moodle-deadlines.json` — every assignment due date from
Moodle's own calendar ajax (`core_calendar_get_calendar_monthly_view`, read-only; an event with no
`action` = already submitted) — and appends each newly fetched file to `data/moodle-new.jsonl`. Both
feed the study page's sync (ohadzvishaboo `sync/study_sync.py`, cron on zima1).
