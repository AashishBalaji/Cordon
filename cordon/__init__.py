"""cordon -- a small multi-tenant service built around one real
constraint: no tenant's load can degrade another tenant's.

See README.md for the full design. In short: a per-tenant resource
budget (budget.py) does the actual isolation work -- deterministic,
no model involved, same "math decides" discipline this author's
other projects use -- while telemetry.py and slo.py turn that
enforcement into something an on-call engineer could actually act
on without drowning in noise.
"""
