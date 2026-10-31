"""Direct tests for estimate_request_cost -- previously only exercised
indirectly through CaseService, which is why mutation testing flagged
it as the weakest-tested function in the project (see README's
"Mutation testing" section for the before/after score)."""

from cordon.models import estimate_request_cost


def test_read_base_cost_with_no_payload():
    assert estimate_request_cost("read", payload_size=0) == 1.0


def test_write_base_cost_with_no_payload():
    assert estimate_request_cost("write", payload_size=0) == 3.0


def test_list_base_cost_with_no_payload():
    assert estimate_request_cost("list", payload_size=0) == 2.0


def test_unknown_operation_falls_back_to_read_cost():
    assert estimate_request_cost("delete", payload_size=0) == 1.0


def test_cost_scales_with_payload_size():
    # 200 bytes -> +1.0 on top of the base cost, by the stated formula
    assert estimate_request_cost("read", payload_size=200) == 2.0
    assert estimate_request_cost("read", payload_size=400) == 3.0


def test_write_is_strictly_more_expensive_than_read_at_same_payload():
    read_cost = estimate_request_cost("read", payload_size=50)
    write_cost = estimate_request_cost("write", payload_size=50)
    assert write_cost > read_cost


def test_list_is_strictly_cheaper_than_write_at_same_payload():
    list_cost = estimate_request_cost("list", payload_size=50)
    write_cost = estimate_request_cost("write", payload_size=50)
    assert list_cost < write_cost
