#!/usr/bin/env python3
"""Sync Venture Dock Luma calendar event stats into a Notion database.

For each event on the calendar this script writes a row to the Notion DB
with counts of invited / approved / checked-in (plus pending, declined,
waitlist). Rows are matched on the "Event API ID" property — existing
rows are updated, new ones created. Notion rows whose event no longer
exists on the Luma calendar are marked Archived (never deleted).

Design notes
------------
* The script is idempotent. Re-running it produces the same Notion state.
* User-editable fields (Notes, Tags, Owner) are never touched.
* For past events older than 7 days, we skip the expensive guest-list
  paginate and reuse the counts already in Notion — those numbers will
  not change. This keeps each run under ~3 minutes and respects Luma's
  aggressive rate limits.
* This is a pre-Supabase bridge. When the Data Ecosystem Architecture's
  Phase 1 is live (Luma → Supabase via webhook), retire this script and
  point Notion at Supabase via Metabase instead.

Env vars
--------
LUMA_API_KEY          required, the Luma calendar API key (secret-…)
NOTION_TOKEN          required, internal-integration token from Notion
NOTION_DATABASE_ID    required, UUID of the Notion database to write to
VENUE_TIMEZONE        optional, defaults to America/Los_Angeles
FRESHNESS_DAYS        optional, default 7. Past events older than this
                      skip the deep-fetch of their guest list.
DRY_RUN               optional, "1" to log changes without writing
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Config

LUMA_API_KEY = os.environ.get("LUMA_API_KEY")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")
VENUE_TZ = ZoneInfo(os.environ.get("VENUE_TIMEZONE", "America/Los_Angeles"))
FRESHNESS_DAYS = int(os.environ.get("FRESHNESS_DAYS", "7"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"

LUMA_BASE = "https://api.lu.ma/public/v1"
NOTION_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

if not LUMA_API_KEY:
    sys.exit("LUMA_API_KEY env var not set")
if not NOTION_TOKEN:
    sys.exit("NOTION_TOKEN env var not set")
if not NOTION_DATABASE_ID:
    sys.exit("NOTION_DATABASE_ID env var not set")

# ---------------------------------------------------------------------------
# Logging helper — write to stderr so workflow logs stay readable.

def log(msg: str) -> None:
    print(f"[{dt.datetime.utcnow().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Rate limiter for Luma. Token bucket at ~3 req/s.

class RateLimiter:
    def __init__(self, rate: float, capacity: int):
        self.rate = rate
        self.capacity = capacity
        self.tokens = float(capacity)
        self.last = time.time()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.time()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
            time.sleep(0.05)


LUMA_RL = RateLimiter(rate=3, capacity=3)
NOTION_RL = RateLimiter(rate=2.5, capacity=3)  # Notion docs say ~3/s


# ---------------------------------------------------------------------------
# HTTP helpers

def http(method: str, url: str, headers: dict, body: dict | None, retries: int = 5) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    last: Exception | None = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = r.read()
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            last = e
            body_text = e.read().decode(errors="replace") if hasattr(e, "read") else ""
            if e.code == 429:
                wait = min(60, 2 ** i + 1)
                log(f"  429 from {url.split('?')[0]}; backing off {wait}s")
                time.sleep(wait)
            elif e.code in (500, 502, 503, 504):
                time.sleep(min(20, 2 ** i))
            else:
                log(f"  HTTP {e.code}: {body_text[:300]}")
                raise
        except Exception as e:
            last = e
            time.sleep(min(20, 2 ** i))
    raise last if last else RuntimeError(f"http exhausted retries: {url}")


def luma_get(path: str, params: dict | None = None) -> dict:
    LUMA_RL.acquire()
    url = LUMA_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "x-luma-api-key": LUMA_API_KEY,
        "User-Agent": "venturedock-sync/1.0",
        "Accept": "application/json",
    }
    return http("GET", url, headers, None)


def notion_request(method: str, path: str, body: dict | None = None) -> dict:
    NOTION_RL.acquire()
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    return http(method, NOTION_BASE + path, headers, body)


# ---------------------------------------------------------------------------
# Luma data fetching

def fetch_all_events() -> list[dict]:
    events: list[dict] = []
    cursor = None
    for _ in range(50):  # safety cap
        params = {}
        if cursor:
            params["pagination_cursor"] = cursor
        data = luma_get("/calendar/list-events", params)
        events.extend(data.get("entries", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return events


def fetch_guest_counts(event_api_id: str) -> dict:
    """Paginate /event/get-guests and tally each status category."""
    counts = {"invited": 0, "approved": 0, "checked_in": 0,
              "pending_approval": 0, "declined": 0, "waitlist": 0,
              "total_guests": 0}
    cursor = None
    pages = 0
    while True:
        params = {"event_api_id": event_api_id}
        if cursor:
            params["pagination_cursor"] = cursor
        data = luma_get("/event/get-guests", params)
        entries = data.get("entries", [])
        counts["total_guests"] += len(entries)
        for g in entries:
            status = (g.get("approval_status") or "").lower()
            if status == "invited":
                counts["invited"] += 1
            elif status == "approved":
                counts["approved"] += 1
            elif status == "pending_approval":
                counts["pending_approval"] += 1
            elif status == "declined":
                counts["declined"] += 1
            elif status == "waitlist":
                counts["waitlist"] += 1
            if g.get("checked_in_at"):
                counts["checked_in"] += 1
        pages += 1
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if pages > 200:  # safety cap, 10k guests
            log(f"  WARN: pagination cap hit for {event_api_id}")
            break
    return counts


# ---------------------------------------------------------------------------
# Notion data access — query existing rows, parse, build {event_api_id → row}

def get_existing_rows() -> dict[str, dict]:
    """Return {event_api_id: page_dict} for everything currently in the DB."""
    rows: dict[str, dict] = {}
    cursor: str | None = None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = notion_request("POST", f"/databases/{NOTION_DATABASE_ID}/query", body)
        for page in data.get("results", []):
            props = page.get("properties", {})
            eid_prop = props.get("Event API ID", {}).get("rich_text") or []
            eid = "".join(p.get("plain_text", "") for p in eid_prop).strip()
            if not eid:
                # row without an API ID — orphan, ignore for matching
                continue
            rows[eid] = page
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return rows


def existing_counts(page: dict) -> dict:
    """Pull current count values out of a Notion page, used to skip stale-deep-fetch."""
    out: dict[str, int | None] = {}
    props = page.get("properties", {})
    for name, key in [
        ("Invited", "invited"),
        ("Approved", "approved"),
        ("Checked-in", "checked_in"),
        ("Pending", "pending_approval"),
        ("Declined", "declined"),
        ("Waitlist", "waitlist"),
        ("Total guests", "total_guests"),
    ]:
        v = props.get(name, {}).get("number")
        out[key] = int(v) if v is not None else None
    return out


# ---------------------------------------------------------------------------
# Status / property builders

def event_status(start_at: dt.datetime | None) -> str:
    if not start_at:
        return "Draft"
    today = dt.datetime.now(dt.timezone.utc).astimezone(VENUE_TZ).date()
    local = start_at.astimezone(VENUE_TZ).date()
    if local < today:
        return "Past"
    if local == today:
        return "Today"
    return "Upcoming"


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def build_props(event: dict, counts: dict, now_iso: str) -> dict:
    start = parse_iso(event.get("start_at"))
    end = parse_iso(event.get("end_at"))
    status = event_status(start)
    visibility = (event.get("visibility") or "public").capitalize()
    if visibility not in ("Public", "Private", "Unlisted"):
        visibility = "Public"

    date_prop: dict = {}
    if start:
        date_prop["start"] = start.astimezone(VENUE_TZ).isoformat()
        if end:
            date_prop["end"] = end.astimezone(VENUE_TZ).isoformat()

    return {
        "Event": {"title": [{"text": {"content": event.get("name") or "(untitled)"}}]},
        "Date": {"date": date_prop or None},
        "Status": {"select": {"name": status}},
        "Visibility": {"select": {"name": visibility}},
        "Invited": {"number": counts["invited"]},
        "Approved": {"number": counts["approved"]},
        "Checked-in": {"number": counts["checked_in"]},
        "Pending": {"number": counts["pending_approval"]},
        "Declined": {"number": counts["declined"]},
        "Waitlist": {"number": counts["waitlist"]},
        "Total guests": {"number": counts["total_guests"]},
        "Luma URL": {"url": event.get("url")},
        "Event API ID": {"rich_text": [{"text": {"content": event.get("api_id") or event.get("id") or ""}}]},
        "Last synced": {"date": {"start": now_iso}},
    }


# ---------------------------------------------------------------------------
# Main sync

def main() -> int:
    log("Starting Luma → Notion sync")
    if DRY_RUN:
        log("DRY_RUN=1 — no writes will be made")

    # 1) Pull Luma events list (cheap)
    luma_events = fetch_all_events()
    log(f"Luma calendar: {len(luma_events)} events")

    # 2) Pull current Notion rows
    existing = get_existing_rows()
    log(f"Notion DB: {len(existing)} existing rows")

    now_utc = dt.datetime.now(dt.timezone.utc)
    now_iso = now_utc.isoformat()
    freshness_cutoff_date = (now_utc - dt.timedelta(days=FRESHNESS_DAYS)).date()

    created = updated = skipped_fresh = skipped_old = errors = 0
    luma_ids = set()

    for i, ev in enumerate(luma_events):
        eid = ev.get("api_id") or ev.get("id")
        if not eid:
            continue
        luma_ids.add(eid)

        start = parse_iso(ev.get("start_at"))
        is_past_stale = (
            start is not None
            and start.astimezone(VENUE_TZ).date() < freshness_cutoff_date
            and eid in existing
        )

        if is_past_stale:
            # Reuse counts already in Notion. We still bump status/dates/etc.
            existing_page = existing[eid]
            ec = existing_counts(existing_page)
            # If any count is None (row was created but never filled), do a fresh fetch
            if any(v is None for v in ec.values()):
                log(f"  [{i+1}/{len(luma_events)}] {eid} past but counts incomplete — deep-fetching")
                try:
                    counts = fetch_guest_counts(eid)
                except Exception as e:
                    log(f"  ERROR fetching {eid}: {e}")
                    errors += 1
                    continue
            else:
                counts = {k: int(v) for k, v in ec.items()}  # type: ignore
                skipped_fresh += 1
        else:
            try:
                counts = fetch_guest_counts(eid)
            except Exception as e:
                log(f"  ERROR fetching {eid}: {e}")
                errors += 1
                continue

        props = build_props(ev, counts, now_iso)

        if eid in existing:
            page_id = existing[eid]["id"]
            if not DRY_RUN:
                try:
                    notion_request("PATCH", f"/pages/{page_id}", {"properties": props})
                except Exception as e:
                    log(f"  ERROR updating Notion page for {eid}: {e}")
                    errors += 1
                    continue
            updated += 1
            if (i + 1) % 10 == 0 or skipped_fresh == 0:
                log(f"  [{i+1}/{len(luma_events)}] updated {eid} ({ev.get('name','')[:40]})")
        else:
            body = {
                "parent": {"database_id": NOTION_DATABASE_ID},
                "properties": props,
            }
            if not DRY_RUN:
                try:
                    notion_request("POST", "/pages", body)
                except Exception as e:
                    log(f"  ERROR creating Notion page for {eid}: {e}")
                    errors += 1
                    continue
            created += 1
            log(f"  [{i+1}/{len(luma_events)}] created {eid} ({ev.get('name','')[:40]})")

    # 3) Archive Notion rows whose event no longer exists on the calendar
    archived = 0
    for eid, page in existing.items():
        if eid in luma_ids:
            continue
        current_status = page.get("properties", {}).get("Status", {}).get("select")
        if current_status and current_status.get("name") == "Archived":
            continue
        if not DRY_RUN:
            try:
                notion_request("PATCH", f"/pages/{page['id']}", {
                    "properties": {
                        "Status": {"select": {"name": "Archived"}},
                        "Last synced": {"date": {"start": now_iso}},
                    },
                })
            except Exception as e:
                log(f"  ERROR archiving {eid}: {e}")
                errors += 1
                continue
        archived += 1
        log(f"  archived {eid}")

    log("Sync complete")
    log(f"  created       : {created}")
    log(f"  updated       : {updated}")
    log(f"  skipped (old) : {skipped_fresh}")
    log(f"  archived      : {archived}")
    log(f"  errors        : {errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
