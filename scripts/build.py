#!/usr/bin/env python3
"""Build the Venture Dock check-in router page.

Reads today's events from the Luma calendar and writes a static
index.html with one big tappable button per event. Each button
deep-links into that event's Luma registration / self-check-in URL.

Run via GitHub Actions on a daily cron (and on manual dispatch).
"""

import datetime as dt
import html
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

API_KEY = os.environ.get("LUMA_API_KEY")
if not API_KEY:
    sys.exit("LUMA_API_KEY env var not set")

BASE_URL = "https://api.lu.ma/public/v1"
VENUE_TZ = ZoneInfo(os.environ.get("VENUE_TIMEZONE", "America/Los_Angeles"))

# Visual / branding knobs — tweak in this block.
BRAND = {
    "name": "Venture Dock",
    "tagline": "Welcome — please check in",
    "color": "#2E5C8A",
    "color_hover": "#244a73",
    "bg": "#F7F4EE",
    "ink": "#1a1a1a",
    "muted": "#6b7280",
    "card_bg": "#ffffff",
}

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "public"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def luma_get(path: str, params: dict | None = None) -> dict:
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "x-luma-api-key": API_KEY,
            "User-Agent": "venturedock-checkin-router/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def fetch_all_events() -> list[dict]:
    events: list[dict] = []
    cursor = None
    for _ in range(50):  # hard safety cap
        params = {}
        if cursor:
            params["pagination_cursor"] = cursor
        data = luma_get("/calendar/list-events", params)
        events.extend(data.get("entries", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return events


def parse_dt(value: str) -> dt.datetime:
    # Luma returns ISO 8601 in UTC, e.g. "2026-05-13T20:30:00.000Z"
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def fmt_time(start: dt.datetime, end: dt.datetime | None) -> str:
    s = start.astimezone(VENUE_TZ).strftime("%-I:%M %p").lower()
    if end:
        e = end.astimezone(VENUE_TZ).strftime("%-I:%M %p").lower()
        return f"{s} – {e}"
    return s


def filter_today(events: list[dict], today: dt.date) -> list[dict]:
    out = []
    for ev in events:
        start_iso = ev.get("start_at")
        if not start_iso:
            continue
        start = parse_dt(start_iso).astimezone(VENUE_TZ)
        if start.date() == today:
            out.append(ev)
    out.sort(key=lambda e: e["start_at"])
    return out


def render(events: list[dict], today: dt.date, generated_at: dt.datetime) -> str:
    today_str = today.strftime("%A, %B %-d, %Y")
    generated_str = generated_at.astimezone(VENUE_TZ).strftime(
        "%b %-d, %-I:%M %p %Z"
    )

    if events:
        cards_html = "\n".join(render_card(ev) for ev in events)
    else:
        cards_html = (
            '<div class="empty">No events scheduled at the Dock today.<br>'
            "If you're here for something, please ask the front desk.</div>"
        )

    return TEMPLATE.format(
        brand=BRAND,
        today=html.escape(today_str),
        cards=cards_html,
        generated=html.escape(generated_str),
        tagline=html.escape(BRAND["tagline"]),
        brand_name=html.escape(BRAND["name"]),
        color=BRAND["color"],
        color_hover=BRAND["color_hover"],
        bg=BRAND["bg"],
        ink=BRAND["ink"],
        muted=BRAND["muted"],
        card_bg=BRAND["card_bg"],
    )


def render_card(ev: dict) -> str:
    name = html.escape(ev.get("name") or "Event")
    url = ev.get("url") or "#"
    start = parse_dt(ev["start_at"])
    end = parse_dt(ev["end_at"]) if ev.get("end_at") else None
    when = html.escape(fmt_time(start, end))
    return f"""\
        <a class="card" href="{html.escape(url)}" target="_top" rel="noopener">
          <div class="card-time">{when}</div>
          <div class="card-name">{name}</div>
          <div class="card-cta">Tap to check in &rarr;</div>
        </a>"""


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<title>{brand_name} · Check in</title>
<style>
  :root {{
    --color: {color};
    --color-hover: {color_hover};
    --bg: {bg};
    --ink: {ink};
    --muted: {muted};
    --card-bg: {card_bg};
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    background: var(--bg);
    color: var(--ink);
    -webkit-font-smoothing: antialiased;
    min-height: 100vh;
    padding: env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
  }}
  main {{
    max-width: 560px;
    margin: 0 auto;
    padding: 28px 20px 60px;
  }}
  header {{ margin-bottom: 28px; }}
  .brand {{
    font-size: 14px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--color);
  }}
  h1 {{
    font-size: 32px;
    line-height: 1.15;
    font-weight: 700;
    margin: 8px 0 4px;
    letter-spacing: -0.02em;
  }}
  .date {{
    color: var(--muted);
    font-size: 16px;
    margin-bottom: 18px;
  }}
  .lede {{
    font-size: 17px;
    color: var(--ink);
    margin: 0 0 24px;
  }}
  .cards {{ display: flex; flex-direction: column; gap: 14px; }}
  .card {{
    display: block;
    background: var(--card-bg);
    border: 1.5px solid rgba(0,0,0,0.06);
    border-left: 5px solid var(--color);
    border-radius: 14px;
    padding: 18px 20px;
    text-decoration: none;
    color: var(--ink);
    transition: transform 80ms ease, border-color 80ms ease, box-shadow 80ms ease;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
  }}
  .card:active {{ transform: scale(0.99); }}
  .card:hover {{ border-color: var(--color); box-shadow: 0 4px 12px rgba(0,0,0,0.08); }}
  .card-time {{
    font-size: 13px;
    font-weight: 600;
    color: var(--color);
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }}
  .card-name {{
    font-size: 19px;
    font-weight: 600;
    line-height: 1.3;
    margin: 6px 0 10px;
  }}
  .card-cta {{
    font-size: 14px;
    color: var(--muted);
  }}
  .empty {{
    background: var(--card-bg);
    border: 1.5px dashed rgba(0,0,0,0.15);
    border-radius: 14px;
    padding: 28px 20px;
    text-align: center;
    color: var(--muted);
    line-height: 1.5;
  }}
  footer {{
    margin-top: 36px;
    text-align: center;
    color: var(--muted);
    font-size: 12px;
  }}
  footer a {{ color: var(--muted); }}
</style>
</head>
<body>
<main>
  <header>
    <div class="brand">{brand_name}</div>
    <h1>{tagline}</h1>
    <div class="date">{today}</div>
    <p class="lede">Tap your event below to register and check in. Takes about 20 seconds.</p>
  </header>

  <div class="cards">
{cards}
  </div>

  <footer>
    Updated {generated} · <a href=".">Reload</a>
  </footer>
</main>
</body>
</html>
"""


def main() -> None:
    now_utc = dt.datetime.now(dt.timezone.utc)
    today = now_utc.astimezone(VENUE_TZ).date()
    print(f"Today (in {VENUE_TZ}): {today}", file=sys.stderr)

    all_events = fetch_all_events()
    print(f"Total events on calendar: {len(all_events)}", file=sys.stderr)

    today_events = filter_today(all_events, today)
    print(f"Events today: {len(today_events)}", file=sys.stderr)
    for ev in today_events:
        print(f"  - {ev['name']} @ {ev['start_at']}", file=sys.stderr)

    html_out = render(today_events, today, now_utc)
    (OUTPUT_DIR / "index.html").write_text(html_out)
    print(f"Wrote {OUTPUT_DIR/'index.html'}", file=sys.stderr)


if __name__ == "__main__":
    main()
