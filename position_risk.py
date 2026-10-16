"""Indicative position sizing for a manual, next-session trading plan."""

import math


def estimate_position(
    equity,
    cash,
    reference_price,
    stop_price,
    candidate_count,
    risk_fraction=0.01,
    board_lot=100,
    cost_buffer=0.01,
):
    """Cap one candidate by portfolio risk, cash, and a standard board lot.

    Prices are references from the completed session. The order must be sized
    again at the actual entry price; a stop can fill worse than its level.
    """
    values = (equity, cash, reference_price, stop_price, risk_fraction, cost_buffer)
    try:
        finite = all(math.isfinite(float(value)) for value in values)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Position inputs must be numeric") from exc
    if not finite:
        raise ValueError("Position inputs must be finite")
    if (
        equity <= 0 or cash < 0 or reference_price <= 0
        or not 0 < stop_price < reference_price
        or not 0 < risk_fraction <= 1 or cost_buffer < 0
        or candidate_count < 1 or board_lot < 1
    ):
        raise ValueError("Invalid position inputs")

    risk_budget = equity * risk_fraction
    loss_per_share = reference_price - stop_price + reference_price * cost_buffer
    cash_per_candidate = cash / candidate_count
    shares_by_risk = math.floor(risk_budget / loss_per_share)
    shares_by_cash = math.floor(
        cash_per_candidate / (reference_price * (1 + cost_buffer))
    )
    shares = max(0, min(shares_by_risk, shares_by_cash))
    shares = shares // board_lot * board_lot
    return {
        "shares": shares,
        "risk_budget": risk_budget,
        "estimated_loss": shares * loss_per_share,
        "estimated_cash": shares * reference_price * (1 + cost_buffer),
    }
