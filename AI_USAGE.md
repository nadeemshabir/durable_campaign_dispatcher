# AI Usage Disclosure

## 1. Tools Used

- Claude Code

## 2. What I Used AI For

- **Understanding the mock provider:** Used AI to read and analyze `server.py` to identify important implementation details and potential traps, including:
  - The two different types of `504` responses.
  - Webhooks arriving before the original `201` response is handled.
  - The provider recording rule rejections (`409`, `422`, and `429`) as violations.
  - The precedence rules used by `/admin/leads`.

- **Planning:** Used AI to explore an initial implementation plan. The initial plan was not included because the implementation evolved. The final design and rationale are documented in `SUBMISSION.md`.

- **Implementation:** Used Claude Code to assist with implementation in `solution/`, including:
  - The dispatcher.
  - Tests.
  - Verification and chaos-testing tools.
  - Running clean tests, injecting failures, and testing `kill -9` recovery across different seeds.

- **Adversarial review:** Used multiple AI reviewers to challenge the implementation against the assignment's rules and failure scenarios. Serious findings were independently rechecked rather than automatically accepted.

- **Simplification:** Used AI assistance to identify opportunities to simplify the final implementation, including a simpler clock design, one fewer intent state, centralized settings, and shorter documentation.

- **Documentation:** Used AI assistance to draft `SUBMISSION.md` and this disclosure.

## 3. What I Wrote or Decided Myself

The important architectural and design decisions were evaluated rather than accepted blindly from AI suggestions.

### Architecture

- Chose a SQLite-backed durable state machine over Temporal.
- Kept the architecture relatively simple for a single-process dispatcher.
- Used SQLite WAL as the source of truth.
- Chose to implement durability, state transitions, retries, and recovery explicitly rather than introducing a workflow orchestration framework.

### Trade-offs

- Used different scheduling margins for fresh calls, retries, and callbacks.
- Decided to retain tenant and phone reservations while an ambiguous provider request was being reconciled.
- Chose to reconcile ambiguous requests using the same `request_id`, rather than creating a new request.
- Accepted temporary resource usage and additional reconciliation complexity in exchange for avoiding duplicate calls.

### Design Principles

The implementation was guided by the following principles:

- Persist intent before contacting the provider.
- Use idempotency to prevent duplicate calls.
- Make concurrency and phone reservations transactional.
- Recover safely after process crashes.
- Handle duplicate, delayed, and out-of-order webhooks.
- Ensure accepted-call accounting happens exactly once.

## 4. Where AI Was Wrong or Unhelpful

AI suggestions were not always correct. Several required debugging, testing, and changes.

### 4.1 Clock Design

An initial midpoint-based `/v1/clock` anchor combined with a monotonic clock caused two `outside_calling_window` violations during a parallel chaos run.

The logs showed that simulated time was approximately 18.8 simulated minutes behind the provider's `sim_now`. A two-second event-loop stall at 1200x speed caused a shift of approximately half the stall multiplied by the simulation speed.

**What changed:**

- Switched to the provider's clock formula using the shared host clock.
- Added a regression test for a stalled clock round trip.
- Added logging when `created_at` deviates beyond the configured margins.
- Removed an unused cross-machine offset cross-check.

### 4.2 Slow Dial Protocol

An initial approach released the slot after a `503` and waited for each `504` to be resolved through a lookup.

One callback ended up 64 hours late. At 1200x simulation speed, a two-second `504` response represents 40 simulated minutes. By the time the callback was shifted, the calling window had closed, moving it to Monday.

**What changed:**

- Resend the same `request_id` after a short timeout.
- Rely on the provider's idempotency guarantee that at most one call is created per key.

### 4.3 Crash Recovery

After a `kill -9` between callback reservation and sending, the initial recovery approach waited three seconds for the system to settle.

At 1200x speed, this represented 60 simulated minutes. One callback was consequently 891 simulated minutes late.

**What changed:**

- Resend using the same `request_id` within the calling window rather than waiting for a long settlement period.

### 4.4 End-of-Window Margin

An AI suggestion initially used a 30-second margin for calls near the end of a window.

However, send-to-accept latency could reach approximately 130 simulated seconds under load. Event-loop stalls of 0.8–1.0 real seconds translated to approximately 500–1,140 simulated seconds at 1200x speed.

There were no calls near the window end in the observed runs. The usual approximately 130-second latency was absorbed by the provider's grace period, not by the margin.

**Final decision:**

- Fresh and retry calls: 2-minute margin.
- Callbacks: 30-second margin.

A two-minute callback margin caused callbacks requested at 20:28 and 20:29 to move to the following morning, so the smaller callback margin was retained deliberately.

### 4.5 Findings from Independent Review

Two additional issues were identified during adversarial review.

**Reset behavior:**

- The initial `--reset` option could wipe dispatcher state on the first run and cause duplicate calls after a `kill -9`.
- Removed the flag.
- The implementation now determines whether to resume or start fresh based on the provider epoch.

**Repeated 4xx reconciliation:**

- The initial reconciler could repeatedly retry a 4xx response after an ambiguous POST.
- An intermediate fix introduced an additional column to keep the intent open.
- The final implementation was simplified: release the intent immediately and remove the extra column.
- The reason is that the provider returns `200` for a request key that already has an existing call. Therefore, a 4xx response means no call exists for that key.

### 4.6 Incorrect Tests

Some generated test assumptions were incorrect:

- A test assumed the first sorted CSV lead would be `ren_0001`, but the actual first lead was `c_acme_feedback`.
- A webhook helper constructed a POST payload without `tenant_id`.

Both were corrected after checking the actual fixtures and provider contract.

### 4.7 Placeholder Assertions

During implementation review, I found placeholder assertions such as:

- `... or True`
- Comparing a list with a copy of itself.

These assertions did not meaningfully verify the expected behavior and were removed.

## 5. Verification

The final implementation was verified at multiple levels.

### Unit Tests

Approximately 120 unit tests using a fake clock, covering:

- Calling-window calculations.
- Next eligible time.
- Campaign priority.
- Outcome precedence.
- Idempotency.
- Crash-orphaned intents.
- `503`, `504`, timeout, and 4xx provider behavior.
- Webhook signatures.

Tests were run using `pytest`.

### End-to-End Verification

Used `solution.tools.verify` against the actual mock provider.

Verification included:

- Zero provider violations.
- Exact comparison of the dispatcher's final lead state against `/admin/leads`.

### Chaos Testing

Used `solution.tools.chaos` to test:

- `kill -9` recovery.
- 600x and 1200x simulation speeds.
- Three times the normal failure rate.
- A final batch using previously unseen seeds.

### Independent Review

Each important rule and failure path was reviewed adversarially. Findings were checked against the implementation, provider behavior, logs, and tests before making changes.

## 6. Final Reflection

AI played a significant role in exploration, implementation assistance, test generation, and adversarial review.

However, its suggestions were not treated as automatically correct. Several recommendations produced incorrect behavior or made the implementation unnecessarily complicated. These were identified through code review, logs, independent review, and failure injection.

The main engineering contribution was making the architectural choices, evaluating trade-offs, challenging incorrect suggestions, and verifying the final behavior against the provider's actual contract and the assignment requirements.

The final design rationale is documented in `SUBMISSION.md`.