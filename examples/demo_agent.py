"""AgentCrash demo agent — a customer-support agent that fails on ambiguity.

Fully offline and deterministic: no external LLM, no network. The "model" is a
pure decision function returning a *policy*, and the agent logic computes the
target from the (possibly counterfactually modified) tool results. This makes
every replay reproducible without API keys — local-first by construction.

Scenario: a customer named "John Smith" (email b@x.com, true id CUST-002) asks
for a refund. Two customers share that name. The BUGGY agent picks the first
search result without verifying identity and refunds the wrong order
(ORD-123, which belongs to CUST-001) -> WrongCustomerError. The FIXED agent
calls ``verify_customer`` against the provided email before refunding, so it
refunds the correct order (ORD-456).

The pivotal decision is the unverified selection after an ambiguous search —
exactly what AgentCrash's disambiguation counterfactual detects: replaying with
only the correct customer averts the failure; only the wrong one reproduces it.
"""

from __future__ import annotations

from typing import Any

# --- The "world" (ground truth the agent does not directly see) ---
CUSTOMERS: list[dict[str, Any]] = [
    {"id": "CUST-001", "name": "John Smith", "email": "a@x.com", "order_id": "ORD-123", "order_total": 250.00},
    {"id": "CUST-002", "name": "John Smith", "email": "b@x.com", "order_id": "ORD-456", "order_total": 89.50},
]
REQUESTER_ID = "CUST-002"  # the true customer making the request


class WrongCustomerError(Exception):
    """Raised when a refund targets an order not owned by the requester."""


# --- Tools (pure, in-memory = the simulated environment for replay) ---
def search_customer(name: str) -> list[dict[str, Any]]:
    """Return all customers matching ``name`` (may be ambiguous)."""
    return [dict(c) for c in CUSTOMERS if c["name"].lower() == name.lower()]


def verify_customer(customer_id: str, email: str) -> bool:
    """True iff this customer is the verified requester."""
    cust = next((c for c in CUSTOMERS if c["id"] == customer_id), None)
    if cust is None:
        return False
    return cust["email"].lower() == email.lower() and customer_id == REQUESTER_ID


def refund_order(order_id: str) -> dict[str, Any]:
    """Process a refund. Raises if the order does not belong to the requester."""
    cust = next((c for c in CUSTOMERS if c["order_id"] == order_id), None)
    if cust is None:
        raise WrongCustomerError(f"unknown order {order_id}")
    if cust["id"] != REQUESTER_ID:
        raise WrongCustomerError(
            f"refunded wrong customer: {order_id} belongs to {cust['id']}, requester is {REQUESTER_ID}"
        )
    return {"order_id": order_id, "refunded": True, "amount": cust["order_total"]}


# --- Agents (authored against the SDK ctx, so they are replayable as-is) ---
def buggy_agent(request: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Picks the first search result without identity verification."""
    results = ctx.tool("search_customer", {"name": request["name"]},
                       lambda: search_customer(request["name"]))
    # Stable signature -> LLM response stays frozen across replays; the agent
    # logic below uses the (possibly modified) results to pick the target.
    ctx.llm({"role": "plan", "policy": "refund_first"}, lambda: {"policy": "refund_first"})
    if not results:
        ctx.decision("give_up", {"reason": "no results"})
        return {"status": "gave_up", "reason": "no results"}
    target = results[0]
    ctx.decision("refund_without_verification", {"order_id": target["order_id"], "verified": False})
    return ctx.tool("refund_order", {"order_id": target["order_id"]},
                    lambda: refund_order(target["order_id"]))


def fixed_agent(request: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Verifies identity against the provided email before refunding."""
    results = ctx.tool("search_customer", {"name": request["name"]},
                       lambda: search_customer(request["name"]))
    ctx.llm({"role": "plan", "policy": "verify_then_refund"},
            lambda: {"policy": "verify_then_refund"})
    email = request.get("email", "")
    verified = None
    for r in results:
        ok = ctx.tool("verify_customer", {"customer_id": r["id"], "email": email},
                      lambda r=r: verify_customer(r["id"], email))
        if ok:
            verified = r
            break
    if verified:
        ctx.decision("refund_after_verification", {"order_id": verified["order_id"], "verified": True})
        return ctx.tool("refund_order", {"order_id": verified["order_id"]},
                        lambda: refund_order(verified["order_id"]))
    ctx.decision("give_up_no_verified_identity", {"reason": "could not verify identity"})
    return {"status": "gave_up", "reason": "could not verify identity"}


DEMO_REQUEST = {"name": "John Smith", "email": "b@x.com", "message": "Please refund my latest order."}