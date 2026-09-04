#!/usr/bin/env python3
"""
TheaterExtras listing watcher.

Polls the members-only listings API and pushes a notification (via ntfy) for
three distinct kinds of change, because on a seatfiller site all three mean
"something is bookable that wasn't a minute ago":

  1. a production that has never appeared in the feed before
  2. a new showtime on a production already in the feed
  3. a showtime whose available_tickets went from 0 to one or more

Configuration comes entirely from environment variables:

  TE_ACCESS_TOKEN       required  your TheaterExtras access token (repo secret)
  NTFY_TOPIC            required  ntfy topic name (repo secret) - keep it unguessable
  NTFY_SERVER           optional  default https://ntfy.sh
  TE_EXCLUDE_REGIONS    optional  comma-separated regions to ignore, default
                                  "Los Angeles". Anything not matched is kept,
                                  so an unfamiliar region label never goes
                                  silently missing.
  ALERT_NEW_SHOWTIMES   optional  default on
  ALERT_TICKET_DROPS    optional  default on
  AVAIL_COOLDOWN_HOURS  optional  re-alert window per showtime (default 12)
  REPEATS               optional  checks per run (default 3)
  SLEEP_SECONDS         optional  seconds between checks in a run (default 120)
  HEARTBEAT_HOURS       optional  alive-ping interval (default 168 = weekly)
  STATE_PATH            optional  default state/seen.json

The token is never printed, logged, or written to the state file.
"""

import datetime
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

API_URL = "https://api.theaterextras.com/account/get-events.php"
SITE = "https://www.theaterextras.com"
PRODUCTION_URL = SITE + "/events/production/?production_id={}"


def env_flag(name, default=True):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


TOKEN = os.environ.get("TE_ACCESS_TOKEN", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip().strip("/")
EXCLUDE_REGIONS = [r.strip().lower() for r in
                   os.environ.get("TE_EXCLUDE_REGIONS", "Los Angeles").split(",")
                   if r.strip()]
ALERT_NEW_SHOWTIMES = env_flag("ALERT_NEW_SHOWTIMES", True)
ALERT_TICKET_DROPS = env_flag("ALERT_TICKET_DROPS", True)
AVAIL_COOLDOWN_HOURS = float(os.environ.get("AVAIL_COOLDOWN_HOURS", "12"))
REPEATS = int(os.environ.get("REPEATS", "3"))
SLEEP_SECONDS = int(os.environ.get("SLEEP_SECONDS", "120"))
HEARTBEAT_HOURS = float(os.environ.get("HEARTBEAT_HOURS", "168"))
STATE_PATH = pathlib.Path(os.environ.get("STATE_PATH", "state/seen.json"))

ERROR_COOLDOWN_HOURS = 6.0   # don't spam the same failure
PRUNE_DAYS = 120             # forget productions unseen this long
MAX_INDIVIDUAL = int(os.environ.get("MAX_INDIVIDUAL", "20"))  # per-show pushes per run

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

REGION_ALIASES = {
    "los angeles": {"los angeles", "losangeles", "los-angeles", "la"},
    "new york": {"new york", "newyork", "new-york", "ny", "nyc"},
}


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt=None):
    return (dt or now()).isoformat(timespec="seconds")


def parse_iso(value):
    try:
        return datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def log(msg):
    print("[{}] {}".format(iso(), msg), flush=True)


# --------------------------------------------------------------------------- state


def load_state():
    if not STATE_PATH.exists():
        return None
    try:
        with STATE_PATH.open(encoding="utf-8") as fh:
            state = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log("WARNING: could not read state file ({}); treating as first run".format(exc))
        return None
    state.setdefault("seen", {})
    return state


def save_state(state):
    state["updated_at"] = iso()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    tmp.replace(STATE_PATH)


def prune(state):
    """Forget long-gone productions, and showtimes that have already happened."""
    cutoff = now() - datetime.timedelta(days=PRUNE_DAYS)
    yesterday = (now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    for key in [k for k, v in state["seen"].items()
                if (parse_iso(v.get("last_seen", "")) or now()) < cutoff]:
        del state["seen"][key]
    for record in state["seen"].values():
        dates = record.get("dates")
        if not isinstance(dates, dict):
            continue
        for date_key in [d for d in dates
                         if re.match(r"^\d{4}-\d{2}-\d{2}", d) and d[:10] < yesterday]:
            del dates[date_key]


# ----------------------------------------------------------------------------- api


def fetch_events():
    """Return the list of production dicts, or raise RuntimeError."""
    payload = json.dumps({"access_token": TOKEN}).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": SITE,
            "Referer": SITE + "/events/",
            "User-Agent": BROWSER_UA,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError("HTTP {} from the listings API".format(exc.code)) from exc
    except Exception as exc:  # network, TLS, timeout, DNS
        raise RuntimeError("could not reach the listings API: {}".format(exc)) from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("API did not return JSON: {!r}".format(raw[:160])) from exc

    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError("API error: {}".format(str(data["error"])[:200]))
    if not isinstance(data, list):
        raise RuntimeError("unexpected API response shape: {!r}".format(raw[:160]))
    return data


# -------------------------------------------------------------------------- filter


def region_values(event):
    """The venue's own region label, falling back to city/state if it has none."""
    theater = event.get("theater") or {}
    region = str(theater.get("region", "") or "").strip().lower()
    if region:
        return region
    return " ".join(
        str(theater.get(key, "") or "") for key in ("city", "state")
    ).strip().lower()


def excluded_region(event):
    """Exclude-list, not include-list: an unfamiliar region label is kept."""
    haystack = region_values(event)
    if not haystack:
        return False
    for region in EXCLUDE_REGIONS:
        candidates = REGION_ALIASES.get(region, set()) | {region}
        if any(re.search(r"\b" + re.escape(alias) + r"\b", haystack)
               for alias in candidates):
            return True
    return False


def filter_region(events):
    if not events or not EXCLUDE_REGIONS:
        return events
    kept = [e for e in events if not excluded_region(e)]
    if not kept:
        log("WARNING: the exclude list {!r} removed all {} events - ignoring it "
            "this run so nothing is missed.".format(EXCLUDE_REGIONS, len(events)))
        return events
    return kept


# ----------------------------------------------------------------------- showtimes


def showtime_key(showtime):
    return str(showtime.get("date", "") or "").strip()[:16]


def available_count(showtime):
    raw = showtime.get("available_tickets")
    if raw is None or raw == "":
        return None                      # unknown, not zero
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def showtimes_now(event):
    """{date_key: available_tickets or None} for every live showtime."""
    out = {}
    for showtime in event.get("showtimes") or []:
        if showtime.get("is_cancelled"):
            continue
        key = showtime_key(showtime)
        if not key:
            continue
        count = available_count(showtime)
        if key in out:
            previous = out[key]
            if previous is not None and count is not None:
                count = max(previous, count)
            elif count is None:
                count = previous
        out[key] = count
    return out


def pretty_date(key):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.datetime.strptime(key, fmt).strftime("%a %b %-d, %-I:%M %p")
        except ValueError:
            continue
    try:
        return datetime.datetime.strptime(key[:10], "%Y-%m-%d").strftime("%a %b %-d")
    except ValueError:
        return key


# --------------------------------------------------------------------- notification


def describe(event):
    theater = event.get("theater") or {}
    venue = str(theater.get("name", "") or "").strip()
    city = str(theater.get("city", "") or "").strip()
    kind = str(event.get("type", "") or "").strip()
    line = " - ".join(b for b in (venue, city) if b)
    if kind:
        line = "{} ({})".format(line, kind) if line else kind
    return line


def _post_ntfy(body):
    request = urllib.request.Request(
        NTFY_SERVER,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        response.read()


def push(title, message, click=None, priority=3, tags=None, state=None, attempts=3):
    """Send one notification. On repeated failure, queue it so it is never lost."""
    if not NTFY_TOPIC:
        log("NTFY_TOPIC is not set - would have sent: {} / {}".format(title, message))
        return True
    body = {"topic": NTFY_TOPIC, "title": title, "message": message,
            "priority": priority, "tags": tags or []}
    if click:
        body["click"] = click
    for attempt in range(attempts):
        try:
            _post_ntfy(body)
            log("pushed: {}".format(title))
            return True
        except Exception as exc:
            log("WARNING: push attempt {} failed ({}): {}".format(attempt + 1, exc, title))
            if attempt < attempts - 1:
                time.sleep(2)
    if state is not None:
        queue = state.setdefault("pending", [])
        queued = dict(body)
        queued["queued_at"] = iso()
        queue.append(queued)
        del queue[:-50]
        log("QUEUED for retry on the next run: {}".format(title))
    return False


def flush_pending(state):
    """Re-send anything a previous run could not deliver."""
    queue = state.get("pending") or []
    if not queue or not NTFY_TOPIC:
        return
    cutoff = now() - datetime.timedelta(hours=48)
    still_failing = []
    for body in queue:
        queued_at = parse_iso(body.get("queued_at", ""))
        if queued_at and queued_at < cutoff:
            log("dropping a queued alert older than 48h: {}".format(body.get("title")))
            continue
        payload = {k: v for k, v in body.items() if k != "queued_at"}
        try:
            _post_ntfy(payload)
            log("re-sent queued alert: {}".format(body.get("title")))
        except Exception as exc:
            log("WARNING: queued alert still failing ({}): {}".format(
                exc, body.get("title")))
            still_failing.append(body)
    state["pending"] = still_failing


def change_lines(change):
    """Human-readable summary of one production's changes."""
    lines = []
    if change["kind"] == "new":
        lines.append(describe(change["event"]))
        dates = sorted(change["dates"])
        if dates:
            lines.append("Next: " + pretty_date(dates[0]))
        added = str(change["event"].get("created_at", "") or "")[:16]
        if added:
            lines.append("Listed: " + pretty_date(added))
    else:
        if change["drops"]:
            for key, count in sorted(change["drops"])[:4]:
                suffix = " ({} left)".format(count) if count else ""
                lines.append("Tickets released: " + pretty_date(key) + suffix)
        if change["new_dates"]:
            shown = [pretty_date(d) for d in sorted(change["new_dates"])[:4]]
            more = len(change["new_dates"]) - len(shown)
            lines.append("New dates: " + ", ".join(shown) +
                         (" +{} more".format(more) if more > 0 else ""))
        lines.append(describe(change["event"]))
    return [l for l in lines if l]


def headline(change):
    if change["kind"] == "new":
        return str(change["event"].get("name", "New listing"))[:120]
    if change["drops"]:
        return "Tickets: " + str(change["event"].get("name", ""))[:110]
    return "New dates: " + str(change["event"].get("name", ""))[:108]


def announce(changes, state=None):
    """One notification per production. New shows go out first."""
    ordered = sorted(changes, key=lambda c: 0 if c["kind"] == "new" else 1)
    for index, change in enumerate(ordered[:MAX_INDIVIDUAL]):
        push(
            title=headline(change),
            message="\n".join(change_lines(change)) or "Something changed",
            click=PRODUCTION_URL.format(change["event"].get("id", "")),
            priority=4,
            tags=["ticket"],
            state=state,
        )
        if index + 1 < min(len(ordered), MAX_INDIVIDUAL):
            time.sleep(0.4)      # be polite to ntfy rather than firing a burst
    overflow = ordered[MAX_INDIVIDUAL:]
    if overflow:
        lines = ["- " + str(c["event"].get("name", "?"))[:60] for c in overflow[:15]]
        if len(overflow) > 15:
            lines.append("...and {} more".format(len(overflow) - 15))
        push(
            title="{} further TheaterExtras updates".format(len(overflow)),
            message="\n".join(lines),
            click=SITE + "/events/",
            priority=4,
            tags=["ticket"],
            state=state,
        )


# ----------------------------------------------------------------------------- run


def record_dates(record, current, stamp):
    """Overwrite the stored showtime map from the live feed."""
    dates = record.setdefault("dates", {})
    for key, count in current.items():
        entry = dates.setdefault(key, {})
        entry["avail"] = count
        entry.setdefault("first_seen", stamp)
    for key in [k for k in dates if k not in current]:
        del dates[key]


def check(state):
    raw = fetch_events()
    events = filter_region(raw)
    log("feed: {} productions total, {} in scope, {} excluded by region".format(
        len(raw), len(events), len(raw) - len(events)))

    # Diagnostics that make a miss diagnosable from the run log alone.
    regions = {}
    for event in raw:
        label = region_values(event) or "(blank)"
        regions[label] = regions.get(label, 0) + 1
    log("region labels: " + ", ".join(
        "{} x{}".format(k, v) for k, v in sorted(regions.items())[:20]))
    newest = sorted(raw, key=lambda e: str(e.get("created_at", "")), reverse=True)[:5]
    log("newest by created_at: " + " | ".join(
        "{} ({})".format(str(e.get("name", ""))[:38], e.get("created_at"))
        for e in newest))

    if not events:
        raise RuntimeError("the API returned an empty list - treating as a failure "
                           "rather than assuming every show was removed")

    seen = state["seen"]
    stamp = iso()
    cooldown = datetime.timedelta(hours=AVAIL_COOLDOWN_HOURS)
    changes = []

    # Diagnostic: if the feed never carries ticket counts, "tickets released"
    # alerts can never fire and the counts below will show it plainly.
    known_counts = unknown_counts = 0
    for event in events:
        for count in showtimes_now(event).values():
            if count is None:
                unknown_counts += 1
            else:
                known_counts += 1
    log("showtimes: {} with a ticket count, {} without".format(
        known_counts, unknown_counts))

    for event in events:
        key = str(event.get("id") or event.get("name") or "")
        if not key:
            continue
        current = showtimes_now(event)

        if key not in seen:
            record = {"name": str(event.get("name", ""))[:200],
                      "first_seen": stamp, "last_seen": stamp}
            record_dates(record, current, stamp)
            seen[key] = record
            changes.append({"kind": "new", "event": event,
                            "dates": list(current), "new_dates": [], "drops": []})
            continue

        record = seen[key]
        record["last_seen"] = stamp
        known = record.get("dates")
        first_time_tracking = not isinstance(known, dict)
        if first_time_tracking:
            # Migrating a production recorded before showtime tracking existed:
            # adopt its current showtimes silently instead of alerting on all of them.
            record_dates(record, current, stamp)
            continue

        new_dates, drops = [], []
        for date_key, count in current.items():
            entry = known.get(date_key)
            if entry is None:
                if ALERT_NEW_SHOWTIMES:
                    new_dates.append(date_key)
                continue
            was = entry.get("avail")
            if (ALERT_TICKET_DROPS and count is not None and count > 0
                    and was is not None and was <= 0):
                last = parse_iso(entry.get("alerted_at", ""))
                if not last or (now() - last) > cooldown:
                    drops.append((date_key, count))
                    entry["alerted_at"] = stamp

        record_dates(record, current, stamp)
        for date_key, _ in drops:
            record["dates"][date_key]["alerted_at"] = stamp

        if new_dates or drops:
            changes.append({"kind": "update", "event": event, "dates": list(current),
                            "new_dates": new_dates, "drops": drops})

    if changes:
        for change in changes:
            log("CHANGE [{}] {}".format(
                change["kind"], str(change["event"].get("name", "?"))[:60]))
        announce(changes, state)


def heartbeat(state):
    if HEARTBEAT_HOURS <= 0:
        return
    last = parse_iso(state.get("last_heartbeat", ""))
    if last and (now() - last) < datetime.timedelta(hours=HEARTBEAT_HOURS):
        return
    state["last_heartbeat"] = iso()
    push(
        title="TheaterExtras watcher is alive",
        message="Tracking {} productions. You will hear from me when a show, a date, "
                "or a batch of tickets appears.".format(len(state["seen"])),
        priority=2,
        tags=["white_check_mark"],
    )


def report_failure(state, message):
    log("ERROR: " + message)
    last = parse_iso(state.get("last_error_push", ""))
    if not last or (now() - last) > datetime.timedelta(hours=ERROR_COOLDOWN_HOURS):
        state["last_error_push"] = iso()
        push(
            title="TheaterExtras watcher needs attention",
            message=message + "\n\nMost likely your access token expired - "
                              "grab a fresh one and update the TE_ACCESS_TOKEN secret.",
            priority=4,
            tags=["warning"],
        )


def seed(state):
    """Record everything currently listed without alerting."""
    flush_pending(state)
    try:
        events = filter_region(fetch_events())
    except RuntimeError as exc:
        state["seed_pending"] = True
        report_failure(state, str(exc))
        save_state(state)
        return 1
    stamp = iso()
    for event in events:
        key = str(event.get("id") or event.get("name") or "")
        if not key:
            continue
        record = {"name": str(event.get("name", ""))[:200],
                  "first_seen": stamp, "last_seen": stamp}
        record_dates(record, showtimes_now(event), stamp)
        state["seen"][key] = record
    state.pop("seed_pending", None)
    state.pop("last_error_push", None)
    state["last_heartbeat"] = iso()
    save_state(state)
    push(
        title="TheaterExtras watcher armed",
        message="Baseline set: {} productions. From here on you get a push for a new "
                "show, a new date, or tickets released.".format(len(state["seen"])),
        priority=3,
        tags=["eyes"],
    )
    log("seeded {} productions; no alerts sent for the baseline".format(len(state["seen"])))
    return 0


def main():
    if not TOKEN:
        log("ERROR: TE_ACCESS_TOKEN is not set.")
        return 1

    state = load_state()
    if state is None:
        return seed({"seen": {}, "created_at": iso()})
    if state.get("seed_pending"):
        return seed(state)

    flush_pending(state)

    failures = 0
    for attempt in range(max(1, REPEATS)):
        try:
            check(state)
            state.pop("last_error_push", None)
        except RuntimeError as exc:
            failures += 1
            report_failure(state, str(exc))
        if attempt < REPEATS - 1:
            time.sleep(SLEEP_SECONDS)

    flush_pending(state)
    prune(state)
    heartbeat(state)
    save_state(state)
    return 1 if failures == REPEATS else 0


if __name__ == "__main__":
    sys.exit(main())
