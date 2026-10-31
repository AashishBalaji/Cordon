"""models.py -- the plain data the service deals with. A case-
management API was picked because it's the simplest stand-in for
Salesforce's own core object (a "Case") that still lets a request's
resource cost vary realistically with what it asks for."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Case:
    case_id: str
    tenant_id: str
    subject: str
    description: str = ""
    status: str = "open"  # open | closed


def estimate_request_cost(operation: str, payload_size: int = 0) -> float:
    """The simulated governor-limit cost of one request. Deliberately
    simple and named as an estimate, not a measurement: a read costs
    less than a write, and cost scales with payload size, the same
    shape (if not the same units) as Apex's own CPU-time/SOQL-row
    accounting. Stated as a plain formula, not hidden in the service
    logic, so a reviewer can see exactly what's being charged for."""
    base_cost = {"read": 1.0, "write": 3.0, "list": 2.0}.get(operation, 1.0)
    return base_cost + (payload_size / 200.0)
