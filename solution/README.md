# Durable Campaign Dispatcher (Assignment B)

One Python process (asyncio) with SQLite as the single source of truth. It survives
`kill -9` at any point because every decision is committed *before* the side effect it
causes, and anything ambiguous keeps holding its concurrency slot and phone until the
provider confirms what happened. Design, decisions and results: [`../SUBMISSION.md`](../SUBMISSION.md).

## Run it (from a clean clone, Linux/macOS/WSL, Python 3.11+)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# terminal 1: the mock provider
cd assignment-b-workflow && python -m mock_telephony

# terminal 2 (repo root): the dispatcher
source .venv/bin/activate
python -m solution.dispatcher
# ... runs until every lead is final, writes ./final_state.csv, exits 0

# check the run the way the graders do (violations + diff against /admin/leads)
python -m solution.tools.verify
```

**After a crash, run the exact same command.** The dispatcher resumes from `dispatcher.db`
if that state belongs to the provider that is running now. If the provider was restarted
(a new run), the old state describes nothing real, so it starts fresh automatically. There
is deliberately no `--reset`: a reset while the provider still has live calls would forget them.

Useful while it runs: `curl -s localhost:8200/status` (per-campaign progress, open calls).

## Tests

```bash
pytest            # ~120 unit tests, a few seconds, fake clock, no network
```

## Chaos run (kill -9, like the graders)

```bash
# self-contained: starts its own mock (seed 4242) and its own dispatcher on their own ports
# (8199, 8299), kills the dispatcher 6 times, then verifies. A mock you started yourself on
# 8099 is never touched.
python -m solution.tools.chaos --start-mock --mock-seed 4242 --mock-port 8199 --port 8299 --kills 6
```

Artifacts (DB, CSV, combined log) go to `solution/runs/` (gitignored). With `--start-mock`,
the tool refuses to run if a provider already answers on `--mock-port`.

## Options

| flag | default | meaning |
|---|---|---|
| `--provider` | `http://127.0.0.1:8099` | provider base URL |
| `--host` / `--port` | `127.0.0.1` / `8200` | webhook listener |
| `--callback-url` | `http://HOST:PORT/webhooks/telephony` | what the provider calls |
| `--db` | `dispatcher.db` | SQLite state |
| `--out` | `final_state.csv` | output |

## Code map

| file | what it does |
|---|---|
| `dispatcher/config.py` | load `campaigns.json` and lead CSVs (customer names are dropped: PII we don't need) |
| `dispatcher/simclock.py` | simulated time computed with the provider's own formula on the shared host clock |
| `dispatcher/rules.py` | **pure** rules: windows (R1), what happens after a call (R4–R9), who to dial next (R2/R3/R6) |
| `dispatcher/store.py` | SQLite schema and every state transition, each in one transaction |
| `dispatcher/provider.py` | HTTP client for the provider; what each POST answer means |
| `dispatcher/dialer.py` | the POST and what each response proves (201/200/503/504/4xx) |
| `dispatcher/reconciler.py` | resolves unknown outcomes and lost webhooks by asking the provider; also crash recovery |
| `dispatcher/webhooks.py` | HMAC-verified result webhook + `/status` |
| `dispatcher/report.py` | writes `final_state.csv` atomically |
| `dispatcher/app.py` | wiring: webhook server, scheduler, reconciler, clock loops |
| `tools/verify.py`, `tools/chaos.py` | grading-style verification and kill -9 runs |
