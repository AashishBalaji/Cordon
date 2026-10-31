# cordon

[![CI](https://github.com/AashishBalaji/cordon/actions/workflows/ci.yml/badge.svg)](https://github.com/AashishBalaji/cordon/actions/workflows/ci.yml)

A small multi-tenant case-management API built around one real
constraint: **no tenant's load can degrade another tenant's.**

Built as a portfolio project targeting Salesforce's "Software
Engineering AMTS" job description (Hyderabad), whose stated asks are
architecting scalable products, building test automation frameworks
(unit/functional, coverage and automation metrics), and using
telemetry/metrics to drive operational excellence. Rather than three
thin, disconnected demos, `cordon` answers all three with one
coherent story, built around the engineering problem that actually
defines Salesforce's own platform: multi-tenancy.

## What it does

A tiny case-management service (`cordon/service.py`, exposed over
HTTP by `cordon/api.py`) where every request is routed through a
**per-tenant resource budget** before it touches anything:

1. `cordon/budget.py` -- two independent token buckets per tenant
   (request-rate, and a simulated resource cost standing in for
   Salesforce's own Apex "governor limits": CPU time, SOQL count,
   heap, combined here into one number). This is the actual
   isolation mechanism: tenants never share a bucket, so there is no
   bucket for a noisy tenant to drain that anyone else depends on.
   Deterministic arithmetic only, no model, same "math decides"
   discipline this author's other projects use.
2. `cordon/telemetry.py` -- every request's outcome (success, error,
   or throttled) recorded per tenant, kept in a rolling window.
3. `cordon/slo.py` -- an SRE-style **error-budget burn rate** per
   tenant, computed from that telemetry. The standard problem with
   naive alerting ("page if error rate > 1%") is alert fatigue:
   thresholds fire on noise until people stop trusting them. Burn
   rate fixes this by asking how *fast* a tenant is spending its
   error budget, not just how many errors happened -- and a tenant
   correctly throttled by its own budget is deliberately excluded
   from this calculation, because that's the system working, not an
   incident.
4. `cordon/api.py` -- a thin FastAPI wrapper, plus `/ops/*` endpoints
   (`headroom`, `burn-rate`, `throttled-fraction`, `sweep`) exposing
   exactly what an on-call engineer would actually want to look at,
   and a real `/metrics` endpoint (`cordon/metrics.py`) in Prometheus
   text exposition format -- the `/ops/*` endpoints are for a human
   clicking around during an incident; `/metrics` is what a real
   monitoring stack (Prometheus, Grafana, an alerting pipeline) would
   actually scrape on an interval.

## Test automation: coverage, mutation testing, and CI enforcing both

65 tests, 99% statement coverage, flake8 clean -- and a GitHub
Actions pipeline (`.github/workflows/ci.yml`, badge above) running
all of it, plus a mutation-testing gate, on every push. Coverage
alone is a weak signal -- a line can be "covered" by a test that
never checks its actual output. So this project also runs **mutation
testing** (`mutmut`, `setup.cfg`): deliberately inject small bugs
into the code (flip an operator, change a constant, swap `<` for
`<=`) and measure what fraction of those injected bugs the test
suite actually catches. That fraction -- the **mutation score** --
is the real automation metric the JD is pointing at with "drive up
code coverage and automation metrics," not coverage alone.

**Real finding, run against the full suite (`python3 -m mutmut
run`):** 513 mutants generated across all of `cordon/`, including
`metrics.py` and `mutation_gate.py` added for the Prometheus endpoint
and the CI gate itself. 398 killed, 115 survived -- **77.6%**. CI now
enforces a **70% floor** on this score on every push
(`cordon/mutation_gate.py`, `scripts/check_mutation_score.py`,
`.github/workflows/ci.yml`): a pull request that weakens the test
suite enough to drop the score below that floor fails the build.

**Honest note on the remaining 115 survivors:** a sample of them
(`python3 -m mutmut show <id>`) turned out to be *equivalent
mutants* -- for example, replacing `now = now if now is not None
else time.monotonic()` with `now = None` in `budget.py`, which
behaves identically because the function it's passed to
(`TokenBucket.available`) re-applies the same `None`-means-"use the
real clock" default one level down. No test can kill a mutant that
doesn't actually change behavior, and chasing 100% here would mean
writing tests that encode the mutation tool's blind spots rather
than anything real about the code. That's also why the CI gate is a
**floor, not a ratchet**: a ratchet that demanded the score only ever
go up would eventually require killing mutants that can't be killed.
77.6% against a reviewed set of survivors, with a floor catching real
regressions, is the honest design, not a chased 100%.

## Telemetry and the noisy-tenant incident

`scripts/simulate_noisy_tenant.py` runs a deliberately staged load
test: `noisy-tenant` sends 80 rapid requests (a runaway client, a
retry loop with no backoff), while `quiet-tenant` sends 15 ordinary
ones. The real, un-fabricated output (`incident/simulation_output.json`,
narrated honestly in `incident/postmortem.md`):

- noisy-tenant: 50/80 succeeded, 30/80 throttled (429s) -- the budget
  doing its job.
- noisy-tenant's SLO burn rate stayed at **0.0** and never paged --
  deliberate: throttled requests are excluded from the SLO
  calculation, because correct containment isn't an incident.
- quiet-tenant: 15/15 succeeded, 0% throttled, budget headroom barely
  moved -- completely unaffected by noisy-tenant's flood, because the
  two tenants never share a bucket.

That's the whole claim this project makes, demonstrated with real
numbers from real code, not asserted in prose: isolation holds, and
the telemetry layer can tell the difference between "a tenant is
being correctly contained" and "something is actually broken."

## Honest scope, stated plainly

- This is a portfolio project built to demonstrate engineering
  judgment on a problem Salesforce's own platform actually has, not
  a claim to have rebuilt Apex's governor limits or any part of
  Salesforce's real infrastructure.
- The simulation in `incident/` is synthetic and single-process --
  real concurrent load, multiple service instances, or real customer
  traffic patterns are out of scope, and the postmortem says so.
- SLO burn-rate alerting here implements one window with one
  threshold -- the core SRE technique, not the full multi-window
  multi-burn-rate design real production systems use (see
  `cordon/slo.py`'s own docstring).
- The in-memory store (`CaseService._cases`) is not a real database;
  this project is about the isolation and telemetry layers, not
  persistence.

## Run it

```
pip install -r requirements.txt
python3 -m pytest -q --cov=cordon --cov-report=term-missing   # tests + coverage
flake8 --max-line-length=100 cordon tests scripts              # lint
python3 -m mutmut run && python3 -m mutmut results             # mutation testing
python3 scripts/simulate_noisy_tenant.py                       # the incident simulation
uvicorn cordon.api:app --reload                                 # run the API itself
```

## Files

| File | What it is |
| --- | --- |
| `cordon/budget.py` | Per-tenant token-bucket isolation -- the core mechanism |
| `cordon/telemetry.py` | Per-tenant request event recording |
| `cordon/slo.py` | SRE-style error-budget burn rate |
| `cordon/service.py` | Case-management logic, wired through budget + telemetry |
| `cordon/api.py` | FastAPI HTTP layer + `/ops/*` endpoints |
| `cordon/models.py` | Plain data + the request-cost formula |
| `tests/` | 49 tests, 99% coverage |
| `setup.cfg` | mutmut configuration |
| `scripts/simulate_noisy_tenant.py` | The incident simulation |
| `incident/postmortem.md` | Honest write-up of that simulation's real output |
