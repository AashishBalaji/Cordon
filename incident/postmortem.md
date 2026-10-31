# Postmortem: noisy-tenant load, simulated

**Status:** synthetic -- this is a deliberately staged load test
against `cordon`'s in-process service, run via
`scripts/simulate_noisy_tenant.py`, not a real production incident
with real customers. The numbers below are real output from real
code run once, deterministically, exactly as printed by that
script (see `incident/simulation_output.json`) -- not hand-picked
or adjusted after the fact.

## What happened

`noisy-tenant` sent 80 requests in rapid succession -- standing in
for a runaway client, a retry loop with no backoff, or a bug in a
tenant's own integration. At the same time, `quiet-tenant` sent 15
ordinary requests.

## What the system did

- **50 of noisy-tenant's 80 requests succeeded; 30 were throttled**
  (HTTP 429 over the real API, `BudgetExceeded` at the service
  layer) -- exactly the request-rate budget doing its job once
  noisy-tenant's bucket ran out.
- **noisy-tenant's SLO burn rate stayed at 0.0, and it did not
  page.** This is deliberate, not a gap: the 30 throttled requests
  were recorded as `THROTTLED`, not `ERROR` (telemetry.py), and
  `slo.py` excludes throttled requests from the error-budget
  calculation on purpose. A tenant correctly contained by its own
  budget is the system working, not an incident -- paging on it
  would train whoever's on call to distrust the alert.
- **quiet-tenant noticed nothing.** 15/15 requests succeeded, 0%
  throttled, and its own budget headroom barely moved (70% of its
  request-rate budget and 91% of its cost budget still available
  afterward) -- unaffected by noisy-tenant's 80-request flood
  because the two tenants never share a bucket.

## What this demonstrates, and what it doesn't

This confirms the isolation mechanism (`budget.py`) and the
telemetry/SLO layer (`telemetry.py` / `slo.py`) do what they claim
on paper: a tenant sending far more load than its budget allows is
contained without being treated as an outage, and a second tenant
sharing the same process is provably unaffected.

What it does **not** demonstrate: behavior across multiple service
instances (there is one process, one `BudgetRegistry`, no distributed
state), or anything about a real customer's real traffic pattern.
Those are the honest limits of a simulation run in a sandbox, not
claims this project makes about production readiness.

## Update: real concurrent load, and a race this surfaced

The gap above originally also listed "behavior under real concurrent
load (this ran single-threaded, sequentially)" as undemonstrated.
`scripts/concurrent_load_test.py` closes it: the same noisy/quiet
scenario, but all 95 requests from both tenants submitted at once to
a 32-worker real `ThreadPoolExecutor`, not a sequential loop. Real
output (`incident/concurrent_load_output.json`): identical result to
the sequential run -- noisy-tenant 50 accepted / 30 throttled,
quiet-tenant 15/15 untouched -- now proven to hold under genuine
concurrent access, not just sequential calls.

**Honest finding from building this:** it didn't hold by default.
`TenantBudget.charge()`'s original check-then-take sequence (read
`available()`, then separately call `take()`) was a real,
reproducible race under actual threads -- 200 concurrent threads
charging a capacity-10 bucket with no refill reliably produced 11+
accepted requests, not 10. `BudgetRegistry.get()`'s lazy
tenant-bucket creation had the same shape of bug: two threads racing
to be "first" for a brand-new tenant could each construct their own
`TenantBudget`, with whichever lost silently discarding a tenant's
in-flight budget state. Both are fixed in `cordon/budget.py` with
plain `threading.Lock`s (a per-tenant lock around `charge()`/
`headroom()`, a short registry-wide lock around first-creation only,
so two different tenants never contend on the same lock) --
`cordon/telemetry.py` got the same treatment for the same reason
(`record()` appending while `events_in_window()` rewrites the same
list in place). `tests/test_concurrency.py` tests each of these
directly with real threads, including reproducing the exact race
shape above against the fixed code to confirm it no longer overdraws.

## If this were real: the root cause and the fix

In a real incident shaped like this, the next step after containment
is asking why noisy-tenant sent 80 requests in the first place --
a bug in their integration, a missing backoff on retries, or a
genuine traffic spike that means their budget should be raised. This
simulation doesn't have a real "why" to investigate, so it stops at
containment, which is the part the system itself is actually
responsible for.
