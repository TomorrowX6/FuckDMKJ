# Early CAPTCHA validation

The user requested completing CAPTCHA before registration opens, then submitting at opening time. The existing Python runner and resident browser remain in use.

## Approach

Resource-only warmup leaves all validation latency after opening. Solving much earlier risks stale results. Use a single bounded solve around T−12 seconds, then consume its result at T0; this implements the approach already requested in conversation.

- The runner owns a result only for the current activity/run. Use the form's CAPTCHA ID when available; otherwise try the existing observed CAPTCHA ID as a hint and require an exact ID match once the form opens.
- Allow at most 10 seconds for the early solve, leaving 2 seconds before opening. Skip the early attempt with less than 5 seconds remaining.
- Accept an early result only within 15 seconds of completion and while the account/session and standardUA remain identical. This is a local freshness limit, not a claim about the provider's token lifetime.
- Early failure, malformed output, changed CAPTCHA ID/session, or stale results fall back to fresh validation after opening. Existing externally supplied validation and dry runs do not trigger early solving.
- Submit only at/after opening; generate timestamp/signature/encryption at submission time. Never reuse a CAPTCHA result for another activity or submission retry. Unknown submission outcomes retain status reconciliation without resubmission.
- Move the worker's read-only connection warmup to T−24 seconds with a cutoff at T−14, so it does not consume the validation window. Preserve cancellation checks before and after validation and the durable submission gate.
- Log early-validation stages, discard reasons, age and elapsed time, never the result/session itself.
- Bind each automatic validation result to the credential snapshot used during solving, and check against the actual request snapshot before entering the durable submit gate. A changed session triggers at most one fresh local solve before any write; repeated changes stop without submission.
- Disable browser tracing because Playwright stores callback return values. Keep structured timing/error events and final status for diagnostics instead.

Actual acceptance of an early result by the registration backend remains unverified without a live registration. Verification must not enroll an activity as a side effect.
