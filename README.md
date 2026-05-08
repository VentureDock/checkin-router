# Venture Dock — Check-in Router

A single static page for the QR code at our front desk. It lists today's
events on the Venture Dock Luma calendar as big tappable buttons. Each
button deep-links to that event's Luma registration / self-check-in URL.

The page is regenerated daily at ~6 AM Pacific by a GitHub Action that
hits the Luma API. Same-day events added later in the day need a manual
"Run workflow" click in the Actions tab.

## How it works

```
front-desk QR  →  https://checkin.venturedock.com/  →  list of today's events  →  tap one  →  Luma registration / check-in
```

- One QR ever — never changes.
- The page behind it changes daily.
- All check-in data still lives in Luma, slotted into the right event.

## Repository layout

```
.
├── .github/workflows/build.yml   GitHub Action: cron + manual trigger + push
├── scripts/build.py              Fetch Luma events, render index.html
├── public/
│   ├── index.html                Generated; what GitHub Pages serves
│   ├── 404.html                  Redirects unknown paths back to /
│   └── CNAME                     Custom domain (edit me)
├── README.md
└── .gitignore
```

## One-time setup

### 1. Create the GitHub repo

Push this directory to a fresh repo (private is fine — Pages still works
on private repos for any paid GitHub plan, otherwise make it public).

```sh
git init
git add .
git commit -m "Initial commit"
git remote add origin git@github.com:<your-org>/checkin-router.git
git push -u origin main
```

### 2. Add the Luma API key as a secret

`Settings → Secrets and variables → Actions → New repository secret`

- Name: `LUMA_API_KEY`
- Value: your Luma API key (the one starting with `secret-…`)

### 3. Enable GitHub Pages

`Settings → Pages → Build and deployment → Source: GitHub Actions`

That's it — no branch configuration; the workflow uploads the artifact
directly.

### 4. Run the workflow once

`Actions → Build & deploy check-in router → Run workflow`

After ~30 seconds you'll have a published page at
`https://<your-org>.github.io/checkin-router/`. Visit it to make sure
today's events render correctly.

### 5. Point your custom domain at it

Pick one approach:

**Option A — subdomain (simplest, recommended):** `checkin.venturedock.com`

1. Edit `public/CNAME` (already pre-filled with `checkin.venturedock.com`
   — change to whatever subdomain you want and commit) and push to main.
2. In your DNS provider, add a CNAME record:
   - Host/Name: `checkin`
   - Value: `<your-org>.github.io`
3. Back in `Settings → Pages`, enter the custom domain and tick
   "Enforce HTTPS" once the cert provisions (≈10 min).

**Option B — path on the apex:** `venturedock.com/check-in`

Pages can't serve under a path of an external domain. You'll need a
reverse proxy on whatever hosts `venturedock.com`:

- **Vercel/Netlify:** add a rewrite from `/check-in/*` →
  `https://checkin.venturedock.com/$1` (or directly to the
  `<your-org>.github.io/checkin-router/` URL).
- **Cloudflare:** a Page Rule or Worker can do the same rewrite.
- **Squarespace/Wix:** these typically don't support arbitrary path
  rewrites — fall back to Option A and set up an HTTP redirect from
  `venturedock.com/check-in` to `checkin.venturedock.com`.

### 6. Print the QR

Generate a QR code pointing at the final URL (`https://checkin.venturedock.com/`
or `https://venturedock.com/check-in`). Any free generator works
(qrcode-monkey.com, qr-code-generator.com, etc.). Print it big at
reception.

## Adding a partner event

1. In Luma, open `Partner Walk-In Template` and click **Duplicate**.
2. Change the name to match the partner event (we use the convention
   `[Walk-in] <event name>`).
3. Set the date and time to match the actual event.
4. Set visibility to **Private** so it doesn't appear on the public
   calendar.
5. Save. The next daily build picks it up automatically; if the event
   is happening today, run the workflow manually from the Actions tab
   to refresh the page right away.

## Customizing the look

Open `scripts/build.py` and edit the `BRAND` dict near the top:

```python
BRAND = {
    "name": "Venture Dock",
    "tagline": "Welcome — please check in",
    "color": "#2E5C8A",       # accent color (button border, highlights)
    "color_hover": "#244a73",
    "bg": "#F7F4EE",          # page background
    "ink": "#1a1a1a",
    "muted": "#6b7280",
    "card_bg": "#ffffff",
}
```

Push, the page rebuilds automatically.

## Local development

```sh
export LUMA_API_KEY=secret-...
python3 scripts/build.py
open public/index.html
```

Python 3.11+ (uses `zoneinfo`). No third-party deps.

## Troubleshooting

**Page shows "No events scheduled" but I have one today.** The script
filters by date in `America/Los_Angeles`; events whose `start_at` is
tomorrow PT won't appear today PT even if "today" UTC has rolled over.
Check the event's start time in Luma.

**Same-day event not showing.** The workflow runs once daily at 13:00
UTC. Hit `Actions → Build & deploy → Run workflow` to force a rebuild.

**API rate limit (429).** Build script makes ~1–3 requests per run, so
this is rare. If it happens, the next scheduled run will retry.

**Want to query a different calendar.** The Luma API key is scoped to
one calendar. Issue a new key for the calendar you want and update the
secret.
