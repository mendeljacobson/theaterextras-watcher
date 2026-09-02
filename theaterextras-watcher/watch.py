#!/usr/bin/env python3
"""
TheaterExtras new-event watcher.

Polls the members-only listings API, compares the set of production IDs against
a committed state file, and sends a push notification (via ntfy) for anything
newly added.

Configuration comes entirely from environment variables:

  TE_ACCESS_TOKEN   required   your TheaterExtras access token (repo secret)
  NTFY_TOPIC        required   ntfy topic name (repo secret) - keep it unguessable
  NTFY_SERVER       optional   default https://ntfy.sh
  TE_REGION         optional   default "New York"; use "all" to watch everything
  REPEATS           optional   checks per run (default 3)
  SLEEP_SECONDS     optional   seconds between checks in a run (default 120)
  HEARTBEAT_HOURS   optional   alive-ping interval (default 168 = weekly)
  STATE_PATH        optional   default state/seen.json

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

TOKEN = os.environ.get("TE_ACCESS_TOKEN", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip().strip("/")
REGION = os.environ.get("TE_REGION", "New York").strip()
REPEATS = int(os.environ.get("REPEATS", "3"))
SLEEP_SECONDS = int(os.environ.get("SLEEP_SECONDS", "120"))
HEARTBEAT_HOURS = float(os.environ.get("HEARTBEAT_HOURS", "168"))
STATE_PATH = pathlib.Path(os.environ.get("STATE_PATH", "state/seen.json"))

ERROR_COOLDOWN_HOURS = 6.0   # don't spam the same failure
PRUNE_DAYS = 120             # forget events unseen this long
MAX_INDIVIDUAL = 3           # more new events than this -> one summary push

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

REGION_ALIASES = {
    "new york": {"new york", "newyork", "new-york", "ny", "nyc"},
    "los angeles": {"los angeles", "losangeles", "los-angeles", "la", "lax"},
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
    cutoff = now() - datetime.timedelta(days=PRUNE_DAYS)
    for key in [k for k, v in state["seen"].items()
                if (parse_iso(v.get("last_seen", "")) or now()) < cutoff]:
        del state["seen"][key]


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


def wanted_region(event):
    if not REGION or REGION.lower() == "all":
        return True
    haystack = region_values(event)
    if not haystack:
        return True  # no region data -> don't silently drop it
    target = REGION.lower()
    candidates = REGION_ALIASES.get(target, set()) | {target}
    # Whole-word matching, so the "ny" alias cannot match inside "Albany".
    return any(re.search(r"\b" + re.escape(alias) + r"\b", haystack)
               for alias in candidates)


def filter_region(events):
    """Filter to the configured region, but fail open if the filter kills everything."""
    if not events:
        return events
    kept = [e for e in events if wanted_region(e)]
    if not kept:
        log("WARNING: region filter {!r} matched 0 of {} events - "
            "ignoring the filter this run so nothing is missed. "
            "Regions seen: {}".format(
                REGION, len(events),
                sorted({region_values(e) or "(blank)" for e in events})[:12]))
        return events
    return kept


# --------------------------------------------------------------------- notification


def describe(event):
    theater = event.get("theater") or {}
    venue = str(theater.get("name", "") or "").strip()
    city = str(theater.get("city", "") or "").strip()
    kind = str(event.get("type", "") or "").strip()
    bits = [b for b in (venue, city) if b]
    line = " - ".join(bits)
    if kind:
        line = "{} ({})".format(line, kind) if line else kind
    return line


def next_showtime(event):
    dates = sorted(
        str(s.get("date", "")) for s in (event.get("showtimes") or [])
        if s.get("date") and not s.get("is_cancelled")
    )
    return dates[0][:16].replace("T", " ") if dates else ""


def push(title, message, click=None, priority=3, tags=None):
    if not NTFY_TOPIC:
        log("NTFY_TOPIC is not set - would have sent: {} / {}".format(title, message))
        return
    body = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags or [],
    }
    if click:
        body["click"] = click
    request = urllib.request.Request(
        NTFY_SERVER,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
        log("pushed: {}".format(title))
    except Exception as exc:
        log("WARNING: push failed ({}): {}".format(exc, title))


def announce(new_events):
    if len(new_events) <= MAX_INDIVIDUAL:
        for event in new_events:
            when = next_showtime(event)
            lines = [describe(event)]
            if when:
                lines.append("Next: " + when)
            push(
                title=str(event.get("name", "New listing"))[:120],
                message="\n".join(l for l in lines if l) or "New listing on TheaterExtras",
                click=PRODUCTION_URL.format(event.get("id", "")),
                priority=4,
                tags=["ticket"],
            )
    else:
        names = ["- " + str(e.get("name", "?"))[:70] for e in new_events[:10]]
        if len(new_events) > 10:
            names.append("...and {} more".format(len(new_events) - 10))
        push(
            title="{} new TheaterExtras listings".format(len(new_events)),
            message="\n".join(names),
            click=SITE + "/events/",
            priority=4,
            tags=["ticket"],
        )


# ----------------------------------------------------------------------------- run


def check(state):
    """One poll. Returns True if the state changed."""
    events = filter_region(fetch_events())
    log("fetched {} events in scope".format(len(events)))
    if not events:
        raise RuntimeError("the API returned an empty list - treating as a failure "
                           "rather than assuming every show was removed")

    seen = state["seen"]
    stamp = iso()
    new_events = []
    for event in events:
        key = str(event.get("id") or event.get("name") or "")
        if not key:
            continue
        if key in seen:
            seen[key]["last_seen"] = stamp
        else:
            seen[key] = {
                "name": str(event.get("name", ""))[:200],
                "first_seen": stamp,
                "last_seen": stamp,
            }
            new_events.append(event)

    if new_events:
        log("NEW: " + " | ".join(str(e.get("name", "?"))[:60] for e in new_events))
        announce(new_events)
    return True


def heartbeat(state):
    last = parse_iso(state.get("last_heartbeat", ""))
    if HEARTBEAT_HOURS <= 0:
        return
    if last and (now() - last) < datetime.timedelta(hours=HEARTBEAT_HOURS):
        return
    state["last_heartbeat"] = iso()
    push(
        title="TheaterExtras watcher is alive",
        message="Tracking {} listings. You will hear from me when something new "
                "shows up.".format(len(state["seen"])),
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


def main():
    if not TOKEN:
        log("ERROR: TE_ACCESS_TOKEN is not set.")
        return 1

    state = load_state()
    first_run = state is None or bool(state.get("seed_pending"))
    if state is None:
        state = {"seen": {}, "created_at": iso()}

    if first_run:
        # Seed silently: everything currently listed counts as already known.
        try:
            events = filter_region(fetch_events())
        except RuntimeError as exc:
            # Never write an empty baseline - that would make every existing
            # listing look "new" on the next run.
            state["seed_pending"] = True
            report_failure(state, str(exc))
            save_state(state)
            return 1
        stamp = iso()
        for event in events:
            key = str(event.get("id") or event.get("name") or "")
            if key:
                state["seen"][key] = {
                    "name": str(event.get("name", ""))[:200],
                    "first_seen": stamp,
                    "last_seen": stamp,
                }
        state.pop("seed_pending", None)
        state.pop("last_error_push", None)
        state["last_heartbeat"] = iso()
        save_state(state)
        push(
            title="TheaterExtras watcher armed",
            message="Baseline set: {} listings. From here on you get a push the "
                    "moment anything new appears.".format(len(state["seen"])),
            priority=3,
            tags=["eyes"],
        )
        log("seeded {} events; no alerts sent for the baseline".format(len(state["seen"])))
        return 0

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

    prune(state)
    heartbeat(state)
    save_state(state)
    return 1 if failures == REPEATS else 0


if __name__ == "__main__":
    sys.exit(main())
