# Early CAPTCHA Implementation Plan

> Execute inline in the existing clean checkout, as requested by the user's direct-execution instructions. Use test-driven development and verification before completion.

**Goal:** Complete a bounded CAPTCHA attempt before opening and reuse only a fresh result belonging to the same activity, CAPTCHA configuration and session.

**Architecture:** Keep the one-shot result local to `EnrollmentRunner.run`. Extend `wait_until` with an early callback and expose bounded solving on the worker's existing solver adapter. Existing browser solving already accepts a timeout.

**Tech Stack:** Python, pytest, existing Playwright CAPTCHA browser and SQLite job store.

**Spec:** `docs/superpowers/specs/2026-11-13-early-captcha-design.md`

## Constraints and review focus

No submission before opening, no duplicate submission on uncertain outcomes, no persisted CAPTCHA results or secrets in logs. Test closed forms, short notice, stale/mismatched results, updated credentials, cancellation, manual validation and dry runs.

## Tasks

- [x] Add failing fake-clock runner tests for early reuse, closed-form hint checks, bounded early failures, freshness/session invalidation, per-run isolation and short-notice/manual/dry-run behavior. Confirmed 11 missing-behavior failures before implementation.
- [x] Implement `wait_until(when, *, prepare=None, prefetch=None)` with T−12 callback and a maximum 10-second budget. Extend `EnrollmentRunner` with optional `captcha_hint` and `captcha_session` inputs; keep result/state local to `run` and check the 15-second freshness limit before consuming. Runner tests: 60 passed.
- [x] Add failing worker tests for earlier connection warmup, bounded solver forwarding and cancellation/redaction. Keep the existing nested adapter, adding `solve(captcha_id, *, timeout=60)` and forwarding `__call__`; pass current stored account/session through the runner's context callback. Confirmed 4 failures before implementation; runner and worker tests: 112 passed. Keeping the adapter nested avoids an unnecessary public class.
- [ ] Update CLI/bot documentation, run the full test suite, review the diff and run staged/private-value plus history secret scans. Commit and push the sanitized source.
- [ ] Deploy the exact published commit only in a quiet window, run server checks, preserve credentials/accounts/grants and verify service health and source hashes. Record deployment evidence outside Git.

## Review findings and fixes

Independent read-only review identified two important issues, both reproduced before fixing:

- A stored-session update in the `captcha_prefetch_reused` hook could pair the old validate with the new request credentials. The worker now verifies the solve-time context against the actual submission snapshot before `begin_submit`; one fresh local solve is allowed, and repeated changes stop before a write. Boundary regression failed before the fix; the runner/worker suite then passed 113 tests.
- Playwright tracing persisted the synthetic validation result from a real local browser page into `browser-trace.zip`. The worker no longer enables tracing; structured event/timing/error diagnostics remain. The artifact regression failed on the archive before the fix; the runner/worker suite then passed 114 tests.

No deferred minor findings. Live enrollment acceptance and provider lifetime remain outside verification: no test enrolls a real activity.
