# TheaterExtras new-listing watcher

Pushes a notification to your phone within about two minutes of a new show
appearing on <https://www.theaterextras.com/events/>.

It runs on GitHub Actions, so it keeps watching when your laptop is closed.

---

## How it works

The events page has no listings in its HTML — the browser loads them from a
members-only API (`POST https://api.theaterextras.com/account/get-events.php`,
authenticated with the `access_token` your browser keeps in local storage).
So a generic "watch this URL" service can't see anything.

This repo calls that API on a schedule with your token, compares the set of
production IDs against `state/seen.json`, and pushes anything it hasn't seen
before. Two guardrails matter:

- **The first run is silent.** It records everything currently listed as the
  baseline, so you don't get 55 notifications on day one.
- **An empty or failed response is treated as breakage, not as "everything was
  removed."** The baseline is never wiped by a bad response, and you get a
  "needs attention" push instead (at most once every 6 hours).

A "still alive" ping arrives weekly. If that stops showing up, something is
wrong — see *Troubleshooting*.

---

## Setup (about 10 minutes, once)

### 1. Install the ntfy app and subscribe

1. Install **ntfy** from the App Store (free, no account).
2. Tap **+** → **Subscribe to topic** → enter exactly:

   ```
   te-alerts-dwvighum5w
   ```

   Leave the server as `ntfy.sh`. That topic name is a random string generated
   for you — treat it like a password, since anyone who knows it can read your
   alerts. Change it if you like; just keep the app and the repo secret in sync.

### 2. Get your TheaterExtras access token

In Chrome, while logged in to TheaterExtras:

1. Open the events page.
2. **View → Developer → Developer Tools** (or ⌥⌘I).
3. **Application** tab → left sidebar **Local Storage** →
   `https://www.theaterextras.com`.
4. Copy the value of the `access_token` row.

Keep this private — it is a login session for your account. It only ever goes
into a GitHub Actions secret, never into the code or the repo.

### 3. Create the repo

1. On GitHub, **New repository** → name it e.g. `theaterextras-watcher`.
2. Set it to **Public**. This matters: public repos get unlimited free Actions
   minutes, while the free private-repo allowance (2,000 min/month) would be
   used up in about two days at this polling rate. Nothing sensitive is in the
   repo — the token and topic live in encrypted secrets, and the only data
   committed is a list of show names.
3. Upload the contents of this folder (drag the unzipped folder's contents onto
   GitHub's **uploading an existing file** page — it preserves the
   `.github/workflows/` path), or push with git:

   ```bash
   git init && git add . && git commit -m "TheaterExtras watcher"
   git branch -M main
   git remote add origin https://github.com/<you>/theaterextras-watcher.git
   git push -u origin main
   ```

### 4. Add the two secrets

Repo → **Settings** → **Secrets and variables** → **Actions** → **New
repository secret**. Add both:

| Name | Value |
| --- | --- |
| `TE_ACCESS_TOKEN` | the token from step 2 |
| `NTFY_TOPIC` | `te-alerts-dwvighum5w` |

### 5. Turn it on

Repo → **Actions** tab → enable workflows if prompted → select
**TheaterExtras watcher** → **Run workflow**.

The first run should finish in under a minute and push "watcher armed" with a
baseline count. If that lands on your phone, you're done — it will now run
itself every 5 minutes, checking three times per run.

---

## Tuning

Edit the `env:` block in `.github/workflows/watch.yml`:

| Variable | Default | Notes |
| --- | --- | --- |
| `TE_REGION` | `New York` | `Los Angeles`, or `all` for both |
| `REPEATS` | `3` | checks per run |
| `SLEEP_SECONDS` | `120` | gap between checks — this is the real polling interval |
| `HEARTBEAT_HOURS` | `168` | weekly alive-ping; `0` disables |

To alert only on certain categories, filter on the `type` field in
`watch.py`'s `check()` — types in the feed look like `Musical`, `Drama`,
`Live Jazz Acts`, `Live Comedy Acts`, `Cabaret`, `Sports`, `Event`.

---

## Troubleshooting

**"Watcher needs attention" push, or the weekly ping stops.**
Almost always an expired token. Redo step 2 and update the `TE_ACCESS_TOKEN`
secret. Nothing else needs touching; the baseline survives.

**Alerts stop silently after a couple of months.**
GitHub disables scheduled workflows in repos with 60 days of no *human*
activity, and the watcher's own commits don't count. If the weekly ping stops
and the token is fine, open the Actions tab and re-enable the workflow (or push
any trivial commit).

**Runs are late.** GitHub's scheduled runs are best-effort and can drift 5-15
minutes at peak times. Real-world latency is usually a few minutes; the
in-run loop is what keeps it tight.

**Notifications feel too quiet.** They're sent at high priority. In iOS
Settings → Notifications → ntfy, allow Time Sensitive notifications and
consider a custom sound in the ntfy app's per-topic settings.
