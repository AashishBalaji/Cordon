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

What it does **not** demonstrate: behavior under real concurrent
load (this ran single-threaded, sequentially), behavior across
multiple service instances (there is one process, one `BudgetRegistry`,
no distributed state), or anything about a real customer's real
traffic pattern. Those are the honest limits of a simulation run in
a sandbox, not claims this project makes about production readiness.

## If this were real: the root cause and the fix

In a real incident shaped like this, the next step after containment
is asking why noisy-tenant sent 80 requests in the first place --
a bug in their integration, a missing backoff on retries, or a
genuine traffic spike that means their budget should be raised. This
simulation doesn't have a real "why" to investigate, so it stops at
containment, which is the part the system itself is actually
responsible for.
