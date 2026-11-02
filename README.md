# Compliance Source Monitor (learning scaffold)

Watches public government pages with Playwright, detects changes, and puts each change in a
**human review queue**. If a page shows a CAPTCHA, OTP prompt, login wall, or returns 403/429, the
worker **stops, takes a screenshot, and opens a handoff** for a person. It never tries to solve or
bypass a challenge.

## Run it

```bash
pip install playwright flask pytest && python -m playwright install chromium

python -m monitor.cli init
python -m monitor.cli add "CBIC notifications" https://example.gov.in/notices \
    --authority CBIC --state CENTRAL --selector "#notice-list" --every 360
python -m monitor.cli run-due        # check every source whose interval has elapsed
python -m monitor.cli serve          # admin UI at http://127.0.0.1:5055
```

Schedule `run-due` with cron (or a GitHub Actions schedule) to make it recurring.
Environment: `MONITOR_DB` (default `monitor.db`), `MONITOR_SNAPSHOTS` (default `snapshots/`).

## Test it

```bash
python -m pytest tests -q    # 26 end-to-end tests: scraping, handoffs, login, CSRF, URL safety
```

## How it works

- **sources** table: every source is a row (URL, CSS selector for the region to watch, interval,
  state, authority). Adding coverage is data, not code. Add from the admin UI or the CLI.
- **Worker** (`monitor/worker.py`): loads the page in Chromium, extracts the watched region's text
  plus its links (so a newly published PDF counts as a change), hashes it, and compares with the
  last snapshot. First run = baseline. Differences become a `pending` change with a diff.
- **Challenge detection** (`monitor/challenge.py`): detects only. Heuristic, so it can miss a new
  challenge style. Flagged sources go to **Handoffs**, one open handoff per source and kind.
- **Politeness**: honours robots.txt (per-source override), identifies itself with a User-Agent,
  and checks at low frequency.
- **Review queue**: approve or reject with a reviewer name. Decisions are final (409 on a re-decide).
- **Errors are loud**: a selector that stops matching, or an unreachable site, sets the source to
  `error` with a message instead of silently reporting "no change".

## What this scaffold does NOT do yet

- Approving a change does not yet update a rules table or notify customers (next step).
- `draft_summary` in `worker.py` is a plain diff summary; plug an LLM in there. Drafts must stay
  reviewable suggestions, never auto-published.
- HTML pages only. It does not download or read PDF contents, only notices PDF links appearing.
- SQLite on the container's disk: on Render's free plan, data resets on every restart or redeploy
  (the demo re-seeds itself). Use a persistent disk or Postgres before keeping real history.
- The private-address block runs when a source is added; it does not stop a redirect to a private address.
- Not tested against real government sites, whose layouts and bot protections vary.

## Deploy (Render, Docker)

`Dockerfile` + `render.yaml` are included. Run with ONE gunicorn worker (it owns the scheduler).

| Variable | Purpose |
|---|---|
| `ADMIN_PASSWORD` | Login password, user is `admin`. Required on Render (the app refuses to start without it). |
| `SEED_DEMO=1` | Add two demo sources (a changing page and a CAPTCHA-gated page) to an empty database. |
| `AUTO_RUN=1` | Run due checks in the background (`SCHEDULER_SECONDS`, default 120). |
| `MONITOR_DB`, `MONITOR_SNAPSHOTS` | Storage paths (the Dockerfile uses `/data`). |

Pages: `/` results, `/sources`, `/review`, `/handoffs`, `/api/results` (JSON), `/healthz` (public), `/demo/*` (public).
