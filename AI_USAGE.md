# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a
skill. Honest answers are never held against you. Discovering undisclosed use is.

> DRAFT prepared with the AI from what happened in the session. Every line below must be
> true for *you*: edit anything that isn't, and fill in the `TODO (you)` parts yourself.

## Tools used
- Claude Code 

## What I used them for
- Reading the repo and the mock provider (`server.py`) closely to find the traps: the two
  different kinds of 504, webhooks arriving before the 201, the provider recording every
  rule rejection (409/422/429) as a violation, the exact `/admin/leads` precedence rules.
- Writing a build plan before any code (not included here: the implementation changed it,
  and the final design is in `SUBMISSION.md`).
- Writing the implementation in `solution/` (dispatcher, tests, `verify` and `chaos` tools)
  and running it against the mock: clean runs, runs with injected failures, and `kill -9`
  drills on several unseen seeds.
- An adversarial review: several AI reviewers, each focused on one rule or failure mode,
  tried to break the code, and each serious claim was re-checked by independent "skeptic"
  reviewers before anything was changed.
- A final audit for unnecessary complexity, run the same way. Its small simplifications were
  applied: a simpler clock, one fewer intent state, settings defined once, shorter documents.
- Drafting `SUBMISSION.md` and this file.

## What I wrote or decided myself
TODO (you): be specific and honest. For example: choosing Assignment B; choosing the
plan's direction (SQLite state machine over Temporal); which trade-offs you accepted
(e.g. margins, holding slots on ambiguity); what you changed after reviewing the plan or
code; anything you pushed back on.

## Where the AI was wrong or unhelpful
These happened in the session:
- **A clock design that broke under load (2 real violations).** The AI anchored simulated
  time to one `/v1/clock` round trip (assume the server read its clock at the midpoint) and
  then advanced it with `time.monotonic()`, arguing this was more robust than the README's
  `real_epoch` formula. A chaos run with many parallel processes failed with 2
  `outside_calling_window` violations. Comparing our logged sim time with the `sim_now` the
  provider put in its rejection showed we were a constant **18.8 sim-minutes behind**: an
  event-loop stall of about 2 s around one sync shifted the anchor by half the stall times
  1200. Fix: use the provider's own formula on the shared host clock (exact, stall-proof),
  add a regression test for a stalled round trip, and log when the provider's `created_at`
  on a 201 is further from our send time than our margins. The AI also kept an offset
  cross-check against `/v1/clock` for a provider on another machine; it never fired in any
  run, and it was removed in the final simplification pass.
- **A design that was correct but too slow at the edges.** The first dial protocol released
  the slot after a 503 and waited for each 504 before looking the call up. A chaos run on a
  hostile seed showed one callback **64 hours late**. The log showed its POST hit the mock's
  2-second 504 (40 simulated minutes at 1200x), the window closed meanwhile, and the
  callback slid to Monday. It was replaced by re-sending the *same* `request_id` after a short
  timeout, which is safe because the provider performs at most one call per key.
- **Crash recovery that was safe but slow.** After a `kill -9` between reserving a callback
  and sending it, recovery waited out a 3 s "settle" (60 sim-min at 1200x) before releasing
  the intent. The window closed meanwhile, and the callback was 891 min late. Now, inside the
  window, recovery re-sends the same `request_id` instead of waiting.
- **An end-of-window margin that was too thin.** The AI picked 30 sim-s. A runtime check it
  added later (provider `created_at` vs our send time) showed send-to-accept latency of
  usually up to ~130 sim-s under load, plus a few machine-wide stalls of 0.8–1.0 s (500–1,140
  sim-s) that no margin covers; none of them came near a window end. The ~130 sim-s was
  absorbed by the provider's grace, not by our margin. The margin is now 2 min for fresh and
  retry calls. Due callbacks keep 30 s as a deliberate trade-off: a 2-min margin pushed
  callbacks asked for 20:28/20:29 to the next morning.
- **Two hazards found only by the independent review.** (1) The AI's first run instructions
  used a `--reset` flag for the first start. A grader re-running the same command after
  `kill -9` would have wiped live state and double-dialled. Fixed: no flag; the dispatcher
  resumes or starts fresh based on the provider's epoch. (2) A 4xx after an ambiguous POST
  could be "resumed" by the reconciler and rejected again, once per pass. Fixed: a rejection
  now releases the intent at once, so nothing is left to re-send. (The first fix kept the
  intent open and stored the rejection in an extra column. The final simplification pass
  removed that: the provider answers 200 for a key that already has a call, so a 4xx means
  the key has none.)
- **Wrong tests.** One test assumed the first row of the sorted CSV was `ren_0001` (it's a
  `c_acme_feedback` lead). A test helper built a webhook body from the POST payload, which
  has no `tenant_id`. Both were caught when the tests failed, and fixed.
- **Placeholder assertions.** The AI wrote two meaningless assertions (`... or True`, and
  comparing a list to a copy of itself) into a test file. They were caught on reading the
  tests and removed.
- TODO (you): add anything you noticed yourself.

## How I verified AI-generated code
- ~120 unit tests with a fake clock (window maths, next eligible time, priority, outcome
  precedence, idempotency, crash-orphaned intents, the 503/504/timeout/4xx protocol,
  webhook signatures) via `pytest`.
- End-to-end runs against the real mock with `solution.tools.verify` (0 violations + exact
  diff against `/admin/leads`), including `kill -9` drills with `solution.tools.chaos` at
  600x and 1200x and at 3x the normal failure rate, and a final batch on seeds never run before.
- Independent adversarial review of every rule and failure path, with each finding
  re-checked before acting on it.
- TODO (you): how you personally checked it, e.g. which files you read line by line,
  what you changed by hand, and whether you can explain `store.py` and `dialer.py` without notes.
