# tau-cli

A connector for Tel Aviv University's student portal (`my.tau.ac.il`). One Python file,
stdlib only, no install step.

The portal is an **OutSystems Reactive** app: every screen talks to JSON `screenservices`
endpoints, and the JavaScript that names them is served to anonymous visitors. That means the
whole API surface is discoverable *without* credentials — `tau map` reads it straight off the
live bundle, so the tool doesn't rot when TAU redeploys.

## Commands

```
tau map [screen] [--json]     list screens + their JSON endpoints (no login needed)
tau screen <name> [--show]    open a screen and dump the data it loads   ← the useful one
tau login                     authenticate, cache the session
tau pniot [number|new]        the inquiry system: list · read one · open the form
tau probe <screen>            call every endpoint with empty inputs (usually rejected)
tau call <path> <apiVersion> [--inputs JSON] [--view NAME]
```

`tau screen Tuition` gives the real account: balance, every transaction, the
outstanding voucher, the army-deposit block.

`map` and `probe` are the reverse-engineering pair: find the screen, dump what it returns,
then promote whatever is useful into a named command.

```bash
tau map Tuition          # → DataActionGetPikuah, DataActionGetTuitionComponents, …
tau probe Tuition        # → the actual numbers
```

## The inquiry system (פניות)

Anything you actually want from a unit goes through TAU's inquiry system — mail sent
straight to a department bounces back closed ("פנייתך נסגרה מכיוון שלא נפתחה דרך מערכת
הפניות"). It is **not** part of the OutSystems portal: it's a FormTitan app
(`tau-int.formtitan.com/ftproject/crm_tau/`) that opens in a second tab, and it's a
SPA — a direct URL to `/my_cases` bounces to Home, so `pniot` clicks its way in.

```bash
tau pniot              # every inquiry: number, status, subject
tau pniot 02798350     # one case: the fields, what was sent, the correspondence
tau pniot new          # opens a VISIBLE browser on the form — you fill and submit
```

`new` deliberately stops at the form: filing is the user's action, not the tool's.
Replies are answered by finding the thread in your mailbox and replying there.

## Why a browser

TAU's portal doesn't authenticate anyone itself — it bounces to the NetIQ SSO at
`nidp.tau.ac.il`, whose form takes **three** fields: username, ID number, password.
And the screenservice endpoints reject empty inputs (most need the student's program
context threaded through), reporting a missing context as "Invalid Login" — the same
message as a bad password, which makes it a trap to debug.

So `screen` drives the real page headlessly and records the JSON it loads, instead of
reconstructing payloads. Login takes ~10s; there's nothing to maintain when TAU
changes a payload shape.

## Credentials

Read from the macOS Keychain — never from argv, a file, or an env var.

| service | account | secret |
|---|---|---|
| `tau` | ID number | portal password |
| `tau-session` | ID number | cached session blob (written by the tool) |

Seed once — the account is the **SSO username**, not the ID:

```bash
security add-generic-password -U -s tau -a <username> -w
```

The ID number is the SSO's third field. It isn't a secret, so it goes in a plain file
rather than the Keychain:

```bash
mkdir -p ~/.config/tau && echo <ID-number> > ~/.config/tau/id
```

It prompts for the password, so nothing lands in shell history. `tau login` refreshes the
cached session; every other command restores it and only re-authenticates when it has expired.

## Notes

- Setup needs Playwright: `uv venv .venv && uv pip install --python .venv/bin/python playwright
  && .venv/bin/python -m playwright install chromium`. Only the login path imports it — `map`
  still runs on bare stdlib. With a venv, run the browser commands as `.venv/bin/python tau …`.
- **Inquiry answers don't reach Gmail** once you have a university user — they go to the TAU
  mailbox and the portal. `tau pniot <number>` is the fast way to see the status; the answer
  text itself may only exist in the TAU mailbox.
- `tau pniot`'s list shows what the portal's default tab shows (recent) — a case rejected
  before it entered the system won't be there at all.
- A welcome interstitial ("Got it, don't show again") sits in front of the login and swallows
  the redirect to the SSO; it has to be dismissed first.
- The SPA holds a socket open, so `networkidle` never fires — waits are on a fixed settle.
- `moduleVersion` is pulled live from `/TAU_Student/moduleservices/moduleinfo`; the per-endpoint
  `apiVersion` keys come from the screen JS. Both change on redeploy — which is why nothing here
  is hardcoded.
- The **bidding** system (`ims.tau.ac.il/Bidd/`) is a separate legacy app with its own login and
  a JS frameset UI. Not covered yet — it only wakes up during a registration round, so it needs
  to be reverse-engineered while one is open.

Personal setup and account specifics stay out of this repo (see the hub).
