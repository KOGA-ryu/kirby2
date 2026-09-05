"""Public-observation assessment rules for the six decision-repeater recipes.

This module deliberately consumes only a validated public frame and a declared
learner response.  It never receives profile/regime labels, a live session, or
private Replay inventory.
"""

from __future__ import annotations

from collections.abc import Mapping

from .practice_episodes import PracticeEpisodeDefinitionV1


def _frame_parts(frame: Mapping[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    book = frame.get("book")
    cursor = frame.get("cursor")
    if not isinstance(book, Mapping) or not isinstance(cursor, Mapping):
        raise ValueError("practice assessment requires a public frame book and cursor")
    return dict(book), dict(cursor)


def _sum_depth(rows: object) -> int | None:
    if not isinstance(rows, list):
        return None
    total = 0
    for row in rows:
        if not isinstance(row, Mapping) or type(row.get("aggregate_quantity")) is not int:
            return None
        total += int(row["aggregate_quantity"])
    return total


def classify_public_reading(
    episode: PracticeEpisodeDefinitionV1,
    frame: Mapping[str, object],
) -> dict[str, object]:
    """Classify F2 exclusively from declared book and trade evidence."""

    if episode.family != "F2_READING":
        raise ValueError("public reading classification requires an F2 recipe")
    book, cursor = _frame_parts(frame)
    trades = frame.get("recent_trades")
    bids = _sum_depth(book.get("bids"))
    asks = _sum_depth(book.get("asks"))
    if not isinstance(trades, list) or bids is None or asks is None:
        status = "INSUFFICIENT_EVIDENCE"
    else:
        rule = episode.observation_rule
        minimum_trades = int(rule["pressure_minimum_recent_trades"])
        minimum_imbalance = int(rule["pressure_minimum_bid_minus_ask_quantity"])
        replenishment_asks = int(rule["replenishment_minimum_ask_quantity"])
        if len(trades) >= minimum_trades and bids - asks >= minimum_imbalance:
            status = "PRESSURE_PRESENT"
        elif asks >= replenishment_asks and asks >= bids:
            status = "REPLENISHMENT_BLOCKS"
        elif not trades:
            status = "NO_OPPORTUNITY"
        else:
            status = "INSUFFICIENT_EVIDENCE"
    return {
        "classification": status,
        "public_evidence": {
            "frame_id": frame.get("frame_id"),
            "cursor_id": cursor.get("cursor_id"),
            "recent_trade_count": None if not isinstance(trades, list) else len(trades),
            "bid_aggregate_quantity": bids,
            "ask_aggregate_quantity": asks,
        },
    }


def residual_precondition(
    episode: PracticeEpisodeDefinitionV1,
    frame: Mapping[str, object],
) -> tuple[bool, list[str]]:
    """Require one public player order to be both filled and still resting."""

    if episode.family != "F3_RESIDUAL":
        return True, []
    account = frame.get("account")
    orders = frame.get("working_orders")
    if not isinstance(account, Mapping) or not isinstance(orders, list):
        return False, []
    valid = [
        row for row in orders
        if isinstance(row, Mapping)
        and type(row.get("filled_quantity")) is int
        and type(row.get("remaining_quantity")) is int
        and int(row["filled_quantity"]) > 0
        and int(row["remaining_quantity"]) > 0
        and isinstance(row.get("order_id"), str)
    ]
    if type(account.get("position")) is not int or int(account["position"]) <= 0:
        return False, [str(row["order_id"]) for row in valid]
    return len(valid) == 1, [str(row["order_id"]) for row in valid]


def assess_response(
    episode: PracticeEpisodeDefinitionV1,
    frame: Mapping[str, object],
    *,
    response_kind: str,
    semantic_action_id: str | None,
    action_index: int,
) -> dict[str, object]:
    """Assess a response before dispatch; it never makes a market mutation."""

    _, cursor = _frame_parts(frame)
    evidence = {
        "frame_id": frame.get("frame_id"),
        "cursor_id": cursor.get("cursor_id"),
        "semantic_action_id": semantic_action_id,
        "order_ids": [],
        "recent_trade_count": None,
        "bid_aggregate_quantity": None,
        "ask_aggregate_quantity": None,
    }
    if episode.family == "F2_READING":
        reading = classify_public_reading(episode, frame)
        evidence.update(reading["public_evidence"])
        observed = str(reading["classification"])
        if observed == "NO_OPPORTUNITY":
            outcome = "NO_OPPORTUNITY" if response_kind in {"WAIT", "DECLINE"} else "FAIL"
        elif observed == "INSUFFICIENT_EVIDENCE":
            outcome = "INSUFFICIENT_EVIDENCE" if response_kind == "INSUFFICIENT_EVIDENCE" else "FAIL"
        else:
            outcome = "PASS" if response_kind == observed else "FAIL"
        return {
            "assessment_class": episode.objective_class,
            "outcome": outcome,
            "action_index": action_index,
            "expected_semantic_action_id": None,
            "observed_public_classification": observed,
            "evidence": evidence,
            "message": "Classification used only the declared public book and recent-trade rule.",
        }
    if response_kind == "DECLINE":
        return {
            "assessment_class": episode.objective_class,
            "outcome": "LEARNER_ABORT",
            "action_index": action_index,
            "expected_semantic_action_id": None,
            "observed_public_classification": None,
            "evidence": evidence,
            "message": "The learner declined this declared practice action.",
        }
    expected = episode.expected_actions[action_index] if action_index < len(episode.expected_actions) else None
    precondition_ok, residual_ids = residual_precondition(episode, frame)
    evidence["order_ids"] = residual_ids
    if not precondition_ok:
        outcome = "INSUFFICIENT_EVIDENCE" if episode.family == "F3_RESIDUAL" else "SYSTEM_FAILURE"
    elif response_kind != "SEMANTIC_ACTION" or semantic_action_id != expected:
        outcome = "FAIL"
    else:
        outcome = "PASS"
    return {
        "assessment_class": episode.objective_class,
        "outcome": outcome,
        "action_index": action_index,
        "expected_semantic_action_id": expected,
        "observed_public_classification": None,
        "evidence": evidence,
        "message": "The declared mechanical step was evaluated against the public anchor frame.",
    }


def assess_dispatched_action(
    episode: PracticeEpisodeDefinitionV1,
    destination_frame: Mapping[str, object],
    *,
    action_index: int,
    command_outcome: Mapping[str, object],
    causal_evidence: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Finish mechanical assessment from the public command result and destination."""

    base = assess_response(
        episode,
        destination_frame,
        response_kind="SEMANTIC_ACTION",
        semantic_action_id=episode.expected_actions[action_index],
        action_index=action_index,
    )
    account = destination_frame.get("account")
    orders = destination_frame.get("working_orders")
    accepted = command_outcome.get("accepted") is True
    expected = episode.expected_actions[action_index]
    condition = accepted and isinstance(account, Mapping) and isinstance(orders, list)
    if expected in {"PLAYER_INCREASE_QUANTITY", "PLAYER_DECREASE_QUANTITY"}:
        condition = condition and account.get("selected_quantity") == episode.observation_rule.get("quantity")
    elif expected == "PLAYER_BUY_BID":
        condition = condition and len(orders) == 1 and account.get("working_order_count") == 1
    elif expected == "PLAYER_CANCEL_NEAREST":
        condition = condition and len(orders) == 0 and account.get("working_order_count") == 0
    elif expected == "PLAYER_REPLACE_NEAREST":
        condition = condition and len(orders) == 1 and account.get("working_order_count") == 1
    base["outcome"] = "PASS" if condition else "FAIL"
    if causal_evidence is not None:
        base["evidence"] = dict(causal_evidence)
    result_order_ids = list(command_outcome.get("resulting_order_ids", []))
    if result_order_ids:
        prior_ids = list(base["evidence"]["order_ids"])
        base["evidence"]["order_ids"] = list(dict.fromkeys([*prior_ids, *result_order_ids]))
    base["message"] = "The actual ordinary command result was checked against public destination evidence."
    return base


def debrief_from_assessment(
    assessment: Mapping[str, object],
    *,
    source_run_id: str,
    action_request_id: str | None,
) -> dict[str, object]:
    """Keep causal debrief links limited to public IDs and declared outcomes."""

    evidence = assessment.get("evidence")
    return {
        "source_run_id": source_run_id,
        "action_request_id": action_request_id,
        "outcome": assessment.get("outcome"),
        "evidence": {} if not isinstance(evidence, Mapping) else dict(evidence),
    }
