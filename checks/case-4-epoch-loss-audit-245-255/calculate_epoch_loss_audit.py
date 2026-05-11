#!/usr/bin/env python3
"""Generate broad epoch loss audit table for Gonka epochs 245-255."""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_FLOOR, getcontext
from pathlib import Path
from typing import Any


getcontext().prec = 50

DEFAULT_NODE_URL = "http://node1.gonka.ai:8000"
DEFAULT_FROM_EPOCH = 245
DEFAULT_TO_EPOCH = 255
DEFAULT_OUTPUT = "checks/case-4-epoch-loss-audit-245-255/epoch_loss_audit_wide_245_255.csv"
DEFAULT_CONFIRMATION_PLUS_POC_SLOT_MINUS_EFFECTIVE_OUTPUT = (
    "checks/case-4-epoch-loss-audit-245-255/"
    "confirmation_plus_poc_slot_minus_effective_reward_245_255.csv"
)
DEFAULT_CHAIN_DELTA_OUTPUT = (
    "checks/case-4-epoch-loss-audit-245-255/chain_expected_delta_245_255.csv"
)
DEFAULT_035_BUG_FIX_DELTA_OUTPUT = (
    "checks/case-4-epoch-loss-audit-245-255/035_bug_fix_expected_minus_actual_245_255.csv"
)
DEFAULT_035_BUG_FIX_EFFECTIVE_WEIGHT_DELTA_OUTPUT = (
    "checks/case-4-epoch-loss-audit-245-255/"
    "035_bug_fix_weight_minus_effective_weight_245_255.csv"
)
DEFAULT_INFERENCE_SLOT_WEIGHT_OUTPUT = (
    "checks/case-4-epoch-loss-audit-245-255/inference_slot_weight_245_255.csv"
)
DEFAULT_PRESERVED_EVENT_WEIGHT_OUTPUT = (
    "checks/case-4-epoch-loss-audit-245-255/preserved_event_weight_245_255.csv"
)
DEFAULT_LARGE_LOSS_THRESHOLD = Decimal("0.5")
DEFAULT_CONFIRMATION_REWARD_FROM_EPOCH = 248
DEFAULT_STUCK_WEIGHT_BASELINE_EPOCH = 248
DEFAULT_STUCK_WEIGHT_FROM_EPOCH = 249
MAX_TABLE_N = 990
P0_MULTIPLIERS = {
    50: (1, 20),
    100: (1, 10),
    200: (1, 5),
    300: (3, 10),
    400: (2, 5),
    500: (1, 2),
}
DECAY_EXPONENTS = {
    Decimal("-0.000475"): Decimal("0.9995251127946402"),
    Decimal("-0.000001"): Decimal("0.9999990000005"),
    Decimal("0.0001"): Decimal("1.0001000050001667"),
    Decimal("0"): Decimal("1"),
}


def fetch_json(
    url: str,
    timeout: int,
    retries: int,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request_headers = {"Accept": "application/json"}
            if headers:
                request_headers.update(headers)
            request = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            http.client.IncompleteRead,
        ) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}: {last_error}") from last_error


def endpoint(node_url: str, path: str) -> str:
    return node_url.rstrip("/") + path


def fetch_historical_json(
    node_url: str,
    path: str,
    block_height: int,
    timeout: int,
    retries: int,
) -> dict[str, Any]:
    return fetch_json(
        endpoint(node_url, path),
        timeout=timeout,
        retries=retries,
        headers={"x-cosmos-block-height": str(block_height)},
    )


def fetch_paginated(
    node_url: str,
    path: str,
    item_key: str,
    timeout: int,
    retries: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    url = endpoint(node_url, path)
    while True:
        payload = fetch_json(url, timeout=timeout, retries=retries)
        items.extend(payload.get(item_key, []))
        next_key = payload.get("pagination", {}).get("next_key")
        if not next_key:
            return items
        url = endpoint(node_url, path) + "?" + urllib.parse.urlencode(
            {"pagination.key": next_key}
        )


def to_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    return int(value)


def decimal_floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def to_gnk(base_units: int) -> str:
    return str(Decimal(base_units) / Decimal(1_000_000_000))


def parse_chain_decimal(value: dict[str, Any]) -> Decimal:
    return Decimal(str(value["value"])) * (Decimal(10) ** int(value["exponent"]))


def calculate_fixed_epoch_reward(epoch: int, params: dict[str, Any]) -> int:
    bitcoin_params = params["bitcoin_reward_params"]
    initial_reward = Decimal(str(bitcoin_params["initial_epoch_reward"]))
    genesis_epoch = int(bitcoin_params["genesis_epoch"])
    decay_rate = parse_chain_decimal(bitcoin_params["decay_rate"])
    epochs_since_genesis = epoch - genesis_epoch
    if epochs_since_genesis <= 0:
        return int(initial_reward)
    exponent = DECAY_EXPONENTS.get(decay_rate)
    if exponent is None:
        raise RuntimeError(f"unsupported decay rate: {decay_rate}")
    return decimal_floor(initial_reward * (exponent ** epochs_since_genesis))


def performance_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["participant_id"]: row for row in rows if row.get("participant_id")}


def preserved_poc_weight(validation_weight: dict[str, Any]) -> int:
    total = 0
    for node in validation_weight.get("ml_nodes", []):
        slots = node.get("timeslot_allocation") or []
        if len(slots) > 1 and bool(slots[1]):
            total += to_int(node.get("poc_weight"))
    return total


def preserved_node_sets(snapshot: dict[str, Any]) -> dict[str, dict[str, set[str]]]:
    by_model: dict[str, dict[str, set[str]]] = {}
    for model_nodes in snapshot.get("model_preserved_nodes", []) or []:
        model_id = model_nodes.get("model_id", "")
        participants = by_model.setdefault(model_id, {})
        for participant in model_nodes.get("participants", []) or []:
            participant_id = participant.get("participant_id")
            if not participant_id:
                continue
            participants[participant_id] = set(participant.get("node_ids", []) or [])
    return by_model


def preserved_weights_from_snapshot(
    snapshot: dict[str, Any],
    model_groups: list[dict[str, Any]],
    coefficients: dict[str, Decimal],
) -> dict[str, int]:
    node_sets = preserved_node_sets(snapshot)
    weights: dict[str, int] = {}
    for group in model_groups:
        model_id = group.get("model_id", "")
        model_node_sets = node_sets.get(model_id, {})
        if not model_node_sets:
            continue
        coefficient = coefficients.get(model_id, Decimal(1))
        for vw in group.get("validation_weights", []):
            address = vw.get("member_address")
            preserved_ids = model_node_sets.get(address, set())
            if not address or not preserved_ids:
                continue
            raw_weight = 0
            for node in vw.get("ml_nodes", []) or []:
                node_id = node.get("node_id") or node.get("nodeId")
                if node_id in preserved_ids:
                    raw_weight += to_int(node.get("poc_weight"))
            if raw_weight:
                weights[address] = weights.get(address, 0) + decimal_floor(
                    Decimal(raw_weight) * coefficient
                )
    return weights


def raw_ml_node_weight(validation_weight: dict[str, Any]) -> int:
    return sum(to_int(node.get("poc_weight")) for node in validation_weight.get("ml_nodes", []))


def model_coefficients(params: dict[str, Any]) -> dict[str, Decimal]:
    coefficients: dict[str, Decimal] = {}
    for model in params.get("poc_params", {}).get("models", []) or []:
        model_id = model.get("model_id")
        weight_scale_factor = model.get("weight_scale_factor")
        if model_id and weight_scale_factor:
            coefficients[model_id] = parse_chain_decimal(weight_scale_factor)
    return coefficients


def coefficient_adjusted_weight(
    validation_weight: dict[str, Any],
    model_id: str,
    coefficients: dict[str, Decimal],
) -> int:
    coefficient = coefficients.get(model_id, Decimal(1))
    return decimal_floor(Decimal(raw_ml_node_weight(validation_weight)) * coefficient)


def node_adjusted_weight(
    node: dict[str, Any],
    model_id: str,
    coefficients: dict[str, Decimal],
) -> int:
    coefficient = coefficients.get(model_id, Decimal(1))
    return decimal_floor(Decimal(to_int(node.get("poc_weight"))) * coefficient)


def build_stuck_weight_deltas(
    groups_by_epoch: dict[int, list[dict[str, Any]]],
    coefficients: dict[str, Decimal],
    baseline_epoch: int,
    affected_from_epoch: int,
    to_epoch: int,
) -> dict[int, dict[str, int]]:
    """Return root-weight deltas for stale pre-v0.2.12 preserved node weights.

    The migration bug kept already-scaled PoC weights in MLNodeInfo.PocWeight.
    Post-v0.2.12 aggregation applied the model coefficient again, so a stuck
    node contributed coefficient * stored_weight instead of stored_weight.
    """
    baseline_nodes: dict[tuple[str, str, str], int] = {}
    for group in groups_by_epoch.get(baseline_epoch, []):
        model_id = group.get("model_id", "")
        if not model_id:
            continue
        for vw in group.get("validation_weights", []):
            address = vw.get("member_address")
            if not address:
                continue
            for node in vw.get("ml_nodes", []) or []:
                node_id = node.get("node_id") or node.get("nodeId")
                baseline_weight = to_int(node.get("poc_weight"))
                if node_id and baseline_weight > 0:
                    baseline_nodes[(model_id, address, node_id)] = baseline_weight

    deltas_by_epoch: dict[int, dict[str, int]] = {}
    for epoch in range(affected_from_epoch, to_epoch + 1):
        for group in groups_by_epoch.get(epoch, []):
            model_id = group.get("model_id", "")
            if not model_id:
                continue
            for vw in group.get("validation_weights", []):
                address = vw.get("member_address")
                if not address:
                    continue
                for node in vw.get("ml_nodes", []) or []:
                    node_id = node.get("node_id") or node.get("nodeId")
                    if not node_id:
                        continue
                    baseline_weight = baseline_nodes.get((model_id, address, node_id))
                    if not baseline_weight:
                        continue
                    current_weight = to_int(node.get("poc_weight"))
                    ratio = Decimal(current_weight) / Decimal(baseline_weight)
                    if Decimal("0.95") <= ratio <= Decimal("1.10"):
                        actual_adjusted = node_adjusted_weight(node, model_id, coefficients)
                        delta = max(0, current_weight - actual_adjusted)
                        if delta:
                            epoch_deltas = deltas_by_epoch.setdefault(epoch, {})
                            epoch_deltas[address] = epoch_deltas.get(address, 0) + delta

    return deltas_by_epoch


def ceil_supported_p0_permille(target: int) -> int:
    if target <= 50:
        return 50
    if target <= 100:
        return 100
    if target <= 200:
        return 200
    if target <= 300:
        return 300
    if target <= 400:
        return 400
    return 500


def dynamic_p0_permille(performance: list[dict[str, Any]], governance_permille: int) -> tuple[int, bool]:
    total_requests = 0
    missed_requests = 0
    participants_used = 0
    for row in performance:
        total = to_int(row.get("inference_count")) + to_int(row.get("missed_requests"))
        if total == 0:
            continue
        total_requests += total
        missed_requests += to_int(row.get("missed_requests"))
        participants_used += 1

    if total_requests < 1000 or participants_used < 5:
        return governance_permille, False

    baseline_permille = missed_requests * 1000 // total_requests
    selected = ceil_supported_p0_permille(min(baseline_permille + 20, 500))
    final_permille = max(governance_permille, selected)
    return final_permille, selected == 500


def missed_stat_test_passed(missed: int, total: int, p0_permille: int) -> bool:
    if total == 0:
        return True
    if missed < 0 or total < 0 or missed > total:
        return False
    if total > MAX_TABLE_N:
        numerator, denominator = P0_MULTIPLIERS[p0_permille]
        return missed * denominator <= total * numerator

    p0 = p0_permille / 1000
    critical = math.floor(total * p0 + 1.6448536269514722 * math.sqrt(total * p0 * (1 - p0)))
    return missed <= min(critical, total)


def apply_power_capping(weights: dict[str, int]) -> dict[str, int]:
    positive = [(address, weight) for address, weight in weights.items() if weight > 0]
    if len(positive) <= 1:
        return weights.copy()

    total_weight = sum(weight for _, weight in positive)
    max_percentage = Decimal("0.30")
    participant_count = len(positive)
    if participant_count == 2:
        max_percentage = Decimal("0.50")
    elif participant_count == 3:
        max_percentage = Decimal("0.40")

    sorted_weights = sorted(weight for _, weight in positive)
    cap: int | None = None
    sum_prev = 0
    for index, current_weight in enumerate(sorted_weights):
        weighted_total = sum_prev + current_weight * (participant_count - index)
        threshold = max_percentage * Decimal(weighted_total)
        if Decimal(current_weight) > threshold:
            numerator = max_percentage * Decimal(sum_prev)
            denominator = Decimal(1) - max_percentage * Decimal(participant_count - index)
            cap = current_weight if denominator <= 0 else decimal_floor(numerator / denominator)
            break
        sum_prev += current_weight

    if cap is None:
        return weights.copy()

    capped = weights.copy()
    for address, weight in positive:
        if weight > cap:
            capped[address] = cap
    return capped


def model_raw_totals(
    model_groups: list[dict[str, Any]],
    coefficients: dict[str, Decimal],
) -> dict[str, int]:
    raw_totals: dict[str, int] = {}
    for group in model_groups:
        model_id = group.get("model_id", "")
        for vw in group.get("validation_weights", []):
            address = vw["member_address"]
            raw_totals[address] = raw_totals.get(address, 0) + coefficient_adjusted_weight(
                vw,
                model_id,
                coefficients,
            )
    return raw_totals


def confirmation_scaled_weight(
    weight: int,
    confirmation_weight: int,
    raw_total: int,
) -> int:
    if raw_total > 0 and weight < raw_total:
        effective_weight = confirmation_weight * weight // raw_total
    else:
        effective_weight = confirmation_weight
    return min(weight, max(0, effective_weight))


def calculate_chain_rewards(
    epoch: int,
    root_group: dict[str, Any],
    model_groups: list[dict[str, Any]],
    coefficients: dict[str, Decimal],
    performance: list[dict[str, Any]],
    excluded_addresses: set[str],
    preserved_by_address: dict[str, int],
    fixed_epoch_reward: int,
    governance_p0_permille: int,
    confirmation_reward_from_epoch: int,
) -> tuple[dict[str, int], dict[str, int]]:
    full_weights: dict[str, int] = {}
    effective_weights: dict[str, int] = {}
    raw_totals = model_raw_totals(model_groups, coefficients)

    for vw in root_group.get("validation_weights", []):
        address = vw["member_address"]
        weight = max(0, to_int(vw.get("weight")))
        full_weights[address] = weight
        if address in excluded_addresses or weight <= 0:
            effective_weights[address] = 0
            continue

        if epoch < confirmation_reward_from_epoch:
            confirmation_weight = max(0, to_int(vw.get("confirmation_weight")))
            effective_weight = min(
                weight,
                max(0, confirmation_weight) + preserved_by_address.get(address, 0),
            )
        else:
            confirmation_weight = max(0, to_int(vw.get("confirmation_weight")))
            effective_weight = confirmation_scaled_weight(
                weight,
                confirmation_weight,
                raw_totals.get(address, 0),
            )
        effective_weights[address] = effective_weight

    participant_weights = apply_power_capping(effective_weights)
    p0_permille, skip_punishment = dynamic_p0_permille(performance, governance_p0_permille)
    performance_by_address = performance_map(performance)
    if not skip_punishment:
        for address, weight in list(participant_weights.items()):
            stats = performance_by_address.get(address, {})
            total = to_int(stats.get("inference_count")) + to_int(stats.get("missed_requests"))
            missed = to_int(stats.get("missed_requests"))
            if not missed_stat_test_passed(missed, total, p0_permille):
                participant_weights[address] = 0

    total_full_weight = sum(full_weights.values())
    expected_rewards: dict[str, int] = {}
    for address, participant_weight in participant_weights.items():
        if total_full_weight > 0 and participant_weight > 0:
            expected_rewards[address] = participant_weight * fixed_epoch_reward // total_full_weight
        else:
            expected_rewards[address] = 0
    return expected_rewards, participant_weights


def load_epoch_groups_by_epoch(args: argparse.Namespace) -> dict[int, list[dict[str, Any]]]:
    groups = fetch_paginated(
        args.node_url,
        "/chain-api/productscience/inference/inference/epoch_group_data",
        "epoch_group_data",
        timeout=args.timeout,
        retries=args.retries,
    )
    by_epoch: dict[int, list[dict[str, Any]]] = {}
    for group in groups:
        epoch = to_int(group.get("epoch_index"))
        if args.from_epoch <= epoch <= args.to_epoch:
            by_epoch.setdefault(epoch, []).append(group)
    return by_epoch


def load_exclusion_reasons(args: argparse.Namespace, epoch: int) -> dict[str, str]:
    payload = fetch_json(
        endpoint(
            args.node_url,
            f"/chain-api/productscience/inference/inference/excluded_participants/{epoch}",
        ),
        timeout=args.timeout,
        retries=args.retries,
    )
    return {
        item["address"]: item.get("reason", "excluded")
        for item in payload.get("items", [])
        if item.get("address")
    }


def load_confirmation_poc_events(args: argparse.Namespace, epoch: int) -> list[dict[str, Any]]:
    payload = fetch_json(
        endpoint(
            args.node_url,
            f"/chain-api/productscience/inference/inference/confirmation_poc_events/{epoch}",
        ),
        timeout=args.timeout,
        retries=args.retries,
    )
    return sorted(
        payload.get("events", []) or [],
        key=lambda event: to_int(event.get("event_sequence")),
    )


def load_preserved_event_weights(
    args: argparse.Namespace,
    epoch: int,
    model_groups: list[dict[str, Any]],
    coefficients: dict[str, Decimal],
) -> list[dict[str, Any]]:
    event_weights: list[dict[str, Any]] = []
    for event in load_confirmation_poc_events(args, epoch):
        generation_start_height = to_int(event.get("generation_start_height"))
        trigger_height = to_int(event.get("trigger_height"))
        snapshot_height = generation_start_height or trigger_height
        if snapshot_height <= 0:
            continue
        payload = fetch_historical_json(
            args.node_url,
            "/chain-api/productscience/inference/inference/preserved_nodes_snapshot",
            snapshot_height,
            timeout=args.timeout,
            retries=args.retries,
        )
        if not payload.get("found"):
            continue
        snapshot = payload.get("snapshot") or {}
        event_weights.append(
            {
                "epoch": epoch,
                "event_sequence": to_int(event.get("event_sequence")),
                "trigger_height": trigger_height,
                "generation_start_height": generation_start_height,
                "episode_anchor_height": to_int(snapshot.get("episode_anchor_height")),
                "weights": preserved_weights_from_snapshot(
                    snapshot,
                    model_groups,
                    coefficients,
                ),
            }
        )
    return event_weights


def zero_reward_reason(
    address: str,
    weight: int,
    confirmation_weight: int,
    effective_weight: int,
    expected_effective: int,
    performance: dict[str, Any],
    exclusion_reasons: dict[str, str],
    p0_permille: int,
    skip_punishment: bool,
) -> str:
    if expected_effective > 0:
        return ""
    if weight <= 0:
        return "zero_weight"
    if address in exclusion_reasons:
        return f"excluded:{exclusion_reasons[address]}"
    if confirmation_weight <= 0:
        return "zero_confirmation_weight"
    if effective_weight <= 0:
        total_requests = to_int(performance.get("inference_count")) + to_int(
            performance.get("missed_requests")
        )
        missed_requests = to_int(performance.get("missed_requests"))
        if not skip_punishment and not missed_stat_test_passed(
            missed_requests,
            total_requests,
            p0_permille,
        ):
            missed_rate = (
                Decimal(missed_requests) * Decimal(100) / Decimal(total_requests)
                if total_requests > 0
                else Decimal(0)
            )
            return f"downtime_punishment({missed_rate.quantize(Decimal('0.01'))}%)"
        return "zero_effective_weight"
    return "zero_expected_reward"


def calculate(args: argparse.Namespace) -> list[dict[str, Any]]:
    params = fetch_json(
        endpoint(args.node_url, "/chain-api/productscience/inference/inference/params"),
        timeout=args.timeout,
        retries=args.retries,
    )["params"]
    coefficients = model_coefficients(params)
    groups_by_epoch = load_epoch_groups_by_epoch(args)
    stuck_weight_deltas = build_stuck_weight_deltas(
        groups_by_epoch,
        coefficients,
        baseline_epoch=args.stuck_weight_baseline_epoch,
        affected_from_epoch=args.stuck_weight_from_epoch,
        to_epoch=args.to_epoch,
    )
    rows: list[dict[str, Any]] = []
    large_loss_threshold = Decimal(str(args.large_loss_threshold))
    validation_params = params.get("validation_params", {})
    governance_p0 = parse_chain_decimal(
        validation_params.get("binom_test_p0", {"value": "1", "exponent": -1})
    )
    governance_p0_permille = ceil_supported_p0_permille(
        int((governance_p0 * Decimal(1000)).to_integral_value(rounding=ROUND_FLOOR))
    )

    for epoch in range(args.from_epoch, args.to_epoch + 1):
        groups = groups_by_epoch.get(epoch, [])
        root_group = next((group for group in groups if group.get("model_id", "") == ""), None)
        if root_group is None:
            continue

        model_groups = [group for group in groups if group.get("model_id", "") != ""]
        preserved_by_address: dict[str, int] = {}
        for group in model_groups:
            for vw in group.get("validation_weights", []):
                preserved_by_address[vw["member_address"]] = preserved_by_address.get(
                    vw["member_address"],
                    0,
                ) + preserved_poc_weight(vw)

        performance = fetch_json(
            endpoint(
                args.node_url,
                f"/chain-api/productscience/inference/inference/epoch_performance_summary/{epoch}",
            ),
            timeout=args.timeout,
            retries=args.retries,
        )["epochPerformanceSummary"]
        performance_by_address = performance_map(performance)
        exclusion_reasons = load_exclusion_reasons(args, epoch)
        excluded_addresses = set(exclusion_reasons)
        p0_permille, skip_punishment = dynamic_p0_permille(
            performance,
            governance_p0_permille,
        )

        total_epoch_weight = to_int(root_group["total_weight"])
        fixed_epoch_reward = calculate_fixed_epoch_reward(epoch, params)
        raw_totals = model_raw_totals(model_groups, coefficients)
        expected_chain_rewards, chain_effective_weights = calculate_chain_rewards(
            epoch=epoch,
            root_group=root_group,
            model_groups=model_groups,
            coefficients=coefficients,
            performance=performance,
            excluded_addresses=excluded_addresses,
            preserved_by_address=preserved_by_address,
            fixed_epoch_reward=fixed_epoch_reward,
            governance_p0_permille=governance_p0_permille,
            confirmation_reward_from_epoch=args.confirmation_reward_from_epoch,
        )

        for vw in root_group.get("validation_weights", []):
            address = vw["member_address"]
            weight = max(0, to_int(vw.get("weight")))
            stuck_weight_delta = stuck_weight_deltas.get(epoch, {}).get(address, 0)
            confirmation_weight = max(0, to_int(vw.get("confirmation_weight")))
            raw_total = raw_totals.get(address, 0)
            effective_weight = chain_effective_weights.get(address, 0)
            actual_reward = to_int(
                performance_by_address.get(address, {}).get("rewarded_coins")
            )
            expected_full = decimal_floor(
                Decimal(weight)
                * Decimal(fixed_epoch_reward)
                / Decimal(total_epoch_weight)
            )
            confirmation_effective_weight = min(weight, confirmation_weight)
            expected_confirmation_weight = decimal_floor(
                Decimal(confirmation_effective_weight)
                * Decimal(fixed_epoch_reward)
                / Decimal(total_epoch_weight)
            )
            poc_slot_weight = preserved_by_address.get(address, 0)
            if epoch < args.stuck_weight_from_epoch:
                full_weight_with_035_bug_fix = weight
                weight_with_035_bug_fix = min(
                    full_weight_with_035_bug_fix,
                    confirmation_weight + poc_slot_weight,
                )
            else:
                full_weight_with_035_bug_fix = weight + stuck_weight_delta
                raw_total_with_035_bug_fix = raw_total + stuck_weight_delta
                weight_with_035_bug_fix = confirmation_scaled_weight(
                    full_weight_with_035_bug_fix,
                    confirmation_weight + stuck_weight_delta,
                    raw_total_with_035_bug_fix,
                )
            expected_035_bug_fix_weight = decimal_floor(
                Decimal(weight_with_035_bug_fix)
                * Decimal(fixed_epoch_reward)
                / Decimal(total_epoch_weight)
            )
            confirmation_plus_poc_slot_weight = min(
                weight,
                confirmation_weight + poc_slot_weight,
            )
            expected_confirmation_plus_poc_slot = decimal_floor(
                Decimal(confirmation_plus_poc_slot_weight)
                * Decimal(fixed_epoch_reward)
                / Decimal(total_epoch_weight)
            )
            expected_effective = expected_chain_rewards.get(address, 0)
            performance_row = performance_by_address.get(address, {})
            lost_vs_full = max(0, expected_full - actual_reward)
            lost_due_to_confirmation = max(0, expected_full - expected_effective)
            chain_expected_delta = expected_effective - actual_reward
            loss_ratio = (
                Decimal(lost_due_to_confirmation) / Decimal(expected_full)
                if expected_full > 0
                else Decimal(0)
            )
            rows.append(
                {
                    "epoch": epoch,
                    "address": address,
                    "weight": weight,
                    "raw_total": raw_total,
                    "stuck_035_weight_delta": stuck_weight_delta,
                    "weight_with_035_bug_fix": weight_with_035_bug_fix,
                    "confirmation_weight": confirmation_weight,
                    "effective_weight": effective_weight,
                    "poc_slot_weight": poc_slot_weight,
                    "preserved_poc_weight": poc_slot_weight,
                    "excluded": address in excluded_addresses,
                    "fixed_epoch_reward_gnk": to_gnk(fixed_epoch_reward),
                    "total_epoch_weight": total_epoch_weight,
                    "actual_reward_gnk": to_gnk(actual_reward),
                    "expected_full_weight_reward_gnk": to_gnk(expected_full),
                    "expected_035_bug_fix_weight_reward_gnk": to_gnk(
                        expected_035_bug_fix_weight
                    ),
                    "expected_confirmation_weight_reward_gnk": to_gnk(
                        expected_confirmation_weight
                    ),
                    "expected_confirmation_plus_poc_slot_reward_gnk": to_gnk(
                        expected_confirmation_plus_poc_slot
                    ),
                    "expected_effective_reward_gnk": to_gnk(expected_effective),
                    "zero_reward_reason": zero_reward_reason(
                        address=address,
                        weight=weight,
                        confirmation_weight=confirmation_weight,
                        effective_weight=effective_weight,
                        expected_effective=expected_effective,
                        performance=performance_row,
                        exclusion_reasons=exclusion_reasons,
                        p0_permille=p0_permille,
                        skip_punishment=skip_punishment,
                    ),
                    "chain_expected_delta_gnk": to_gnk(chain_expected_delta),
                    "lost_vs_full_weight_gnk": to_gnk(lost_vs_full),
                    "lost_due_to_confirmation_weight_gnk": to_gnk(
                        lost_due_to_confirmation
                    ),
                    "confirmation_loss_ratio": str(loss_ratio),
                    "zero_paid_with_positive_expected": bool(
                        actual_reward == 0 and expected_full > 0
                    ),
                    "large_confirmation_loss": bool(
                        loss_ratio >= large_loss_threshold and lost_due_to_confirmation > 0
                    ),
                    "chain_reward_matches_actual": actual_reward == expected_effective,
                }
            )

    return sorted(
        rows,
        key=lambda row: (
            int(row["epoch"]),
            Decimal(row["lost_vs_full_weight_gnk"]),
            row["address"],
        ),
        reverse=True,
    )


def preserved_event_weight_wide_rows(
    args: argparse.Namespace,
    base_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    params = fetch_json(
        endpoint(args.node_url, "/chain-api/productscience/inference/inference/params"),
        timeout=args.timeout,
        retries=args.retries,
    )["params"]
    coefficients = model_coefficients(params)
    groups_by_epoch = load_epoch_groups_by_epoch(args)
    event_weights_by_epoch: dict[int, list[dict[str, Any]]] = {}
    addresses: set[str] = set()
    legacy_weights: dict[str, dict[int, int]] = {}
    if base_rows is not None:
        for row in base_rows:
            address = row["address"]
            addresses.add(address)
            epoch = int(row["epoch"])
            if epoch <= args.confirmation_reward_from_epoch:
                weight = int(row["poc_slot_weight"])
                if weight:
                    legacy_weights.setdefault(address, {})[epoch] = weight

    event_from_epoch = max(args.from_epoch, args.confirmation_reward_from_epoch + 1)
    for epoch in range(event_from_epoch, args.to_epoch + 1):
        model_groups = [
            group
            for group in groups_by_epoch.get(epoch, [])
            if group.get("model_id", "") != ""
        ]
        if not model_groups:
            continue
        event_weights = load_preserved_event_weights(
            args,
            epoch,
            model_groups,
            coefficients,
        )
        event_weights_by_epoch[epoch] = event_weights
        for event in event_weights:
            addresses.update(event["weights"])

    rows: list[dict[str, Any]] = []
    for address in sorted(addresses):
        row: dict[str, Any] = {"address": address}
        total = 0
        for epoch in range(args.from_epoch, args.to_epoch + 1):
            for index, event in enumerate(event_weights_by_epoch.get(epoch, []), start=1):
                row[f"epoch_{epoch}_event_{index}_sequence"] = event["event_sequence"]
                row[f"epoch_{epoch}_event_{index}_trigger_height"] = event["trigger_height"]
                row[f"epoch_{epoch}_event_{index}_generation_start_height"] = event[
                    "generation_start_height"
                ]
                row[f"epoch_{epoch}_event_{index}_episode_anchor_height"] = event[
                    "episode_anchor_height"
                ]
                weight = int(event["weights"].get(address, 0))
                total += weight
                row[f"epoch_{epoch}_event_{index}_preserved_weight"] = (
                    "" if weight == 0 else weight
                )
        row["total_preserved_event_weight"] = "" if total == 0 else total
        rows.append(row)

    if not rows:
        return [{"address": "", "total_preserved_event_weight": ""}]

    field_order = ["address"]
    for epoch in range(args.from_epoch, args.to_epoch + 1):
        if epoch <= args.confirmation_reward_from_epoch:
            field_order.append(f"epoch_{epoch}_poc_slot_allocation_weight")
            continue
        for index, _event in enumerate(event_weights_by_epoch.get(epoch, []), start=1):
            field_order.append(f"epoch_{epoch}_event_{index}_preserved_weight")
    field_order.append("total_preserved_event_weight")

    for row in rows:
        address = row["address"]
        for epoch in range(args.from_epoch, min(args.to_epoch, args.confirmation_reward_from_epoch) + 1):
            weight = legacy_weights.get(address, {}).get(epoch, 0)
            row[f"epoch_{epoch}_poc_slot_allocation_weight"] = "" if weight == 0 else weight
            if weight:
                row["total_preserved_event_weight"] = int(row["total_preserved_event_weight"] or 0) + weight

    ordered_rows = [{field: row.get(field, "") for field in field_order} for row in rows]
    return sorted(
        ordered_rows,
        key=lambda row: (int(row["total_preserved_event_weight"] or 0), row["address"]),
        reverse=True,
    )


def sum_decimal_strings(rows: list[dict[str, Any]], field: str) -> Decimal:
    return sum((Decimal(str(row[field])) for row in rows), Decimal(0))


def blank_zero_decimal(value: Decimal) -> str:
    return "" if value == 0 else str(value)


def wide_rows(
    long_rows: list[dict[str, Any]],
    from_epoch: int,
    to_epoch: int,
) -> list[dict[str, Any]]:
    by_address: dict[str, dict[int, dict[str, Any]]] = {}
    for row in long_rows:
        by_address.setdefault(row["address"], {})[int(row["epoch"])] = row

    rows: list[dict[str, Any]] = []
    for address, by_epoch in by_address.items():
        participant_rows = list(by_epoch.values())
        wide: dict[str, Any] = {
            "address": address,
            "total_lost_vs_full_weight_gnk": str(
                sum_decimal_strings(participant_rows, "lost_vs_full_weight_gnk")
            ),
            "total_lost_due_to_confirmation_weight_gnk": str(
                sum_decimal_strings(
                    participant_rows,
                    "lost_due_to_confirmation_weight_gnk",
                )
            ),
            "epochs_with_zero_paid_positive_expected": sum(
                1 for row in participant_rows if row["zero_paid_with_positive_expected"]
            ),
            "epochs_with_large_confirmation_loss": sum(
                1 for row in participant_rows if row["large_confirmation_loss"]
            ),
        }

        for epoch in range(from_epoch, to_epoch + 1):
            row = by_epoch.get(epoch)
            prefix = f"epoch_{epoch}"
            if row is None:
                wide.update(
                    {
                        f"{prefix}_weight": "",
                        f"{prefix}_raw_total": "",
                        f"{prefix}_stuck_035_weight_delta": "",
                        f"{prefix}_weight_with_035_bug_fix": "",
                        f"{prefix}_confirmation_weight": "",
                        f"{prefix}_effective_weight": "",
                        f"{prefix}_poc_slot_weight": "",
                        f"{prefix}_excluded": "",
                        f"{prefix}_actual_reward_gnk": "",
                        f"{prefix}_expected_full_weight_reward_gnk": "",
                        f"{prefix}_expected_035_bug_fix_weight_reward_gnk": "",
                        f"{prefix}_expected_confirmation_weight_reward_gnk": "",
                        f"{prefix}_expected_confirmation_plus_poc_slot_reward_gnk": "",
                        f"{prefix}_expected_effective_reward_gnk": "",
                        f"{prefix}_zero_reward_reason": "",
                        f"{prefix}_chain_expected_delta_gnk": "",
                        f"{prefix}_lost_vs_full_weight_gnk": "",
                        f"{prefix}_lost_due_to_confirmation_weight_gnk": "",
                        f"{prefix}_confirmation_loss_ratio": "",
                        f"{prefix}_zero_paid_with_positive_expected": "",
                        f"{prefix}_large_confirmation_loss": "",
                    }
                )
                continue

            wide.update(
                {
                    f"{prefix}_weight": row["weight"],
                    f"{prefix}_raw_total": row["raw_total"],
                    f"{prefix}_stuck_035_weight_delta": row["stuck_035_weight_delta"],
                    f"{prefix}_weight_with_035_bug_fix": row[
                        "weight_with_035_bug_fix"
                    ],
                    f"{prefix}_confirmation_weight": row["confirmation_weight"],
                    f"{prefix}_effective_weight": row["effective_weight"],
                    f"{prefix}_poc_slot_weight": row["poc_slot_weight"],
                    f"{prefix}_excluded": row["excluded"],
                    f"{prefix}_actual_reward_gnk": row["actual_reward_gnk"],
                    f"{prefix}_expected_full_weight_reward_gnk": row[
                        "expected_full_weight_reward_gnk"
                    ],
                    f"{prefix}_expected_035_bug_fix_weight_reward_gnk": row[
                        "expected_035_bug_fix_weight_reward_gnk"
                    ],
                    f"{prefix}_expected_confirmation_weight_reward_gnk": row[
                        "expected_confirmation_weight_reward_gnk"
                    ],
                    f"{prefix}_expected_confirmation_plus_poc_slot_reward_gnk": row[
                        "expected_confirmation_plus_poc_slot_reward_gnk"
                    ],
                    f"{prefix}_expected_effective_reward_gnk": row[
                        "expected_effective_reward_gnk"
                    ],
                    f"{prefix}_zero_reward_reason": row["zero_reward_reason"],
                    f"{prefix}_chain_expected_delta_gnk": row[
                        "chain_expected_delta_gnk"
                    ],
                    f"{prefix}_lost_vs_full_weight_gnk": row[
                        "lost_vs_full_weight_gnk"
                    ],
                    f"{prefix}_lost_due_to_confirmation_weight_gnk": row[
                        "lost_due_to_confirmation_weight_gnk"
                    ],
                    f"{prefix}_confirmation_loss_ratio": row[
                        "confirmation_loss_ratio"
                    ],
                    f"{prefix}_zero_paid_with_positive_expected": row[
                        "zero_paid_with_positive_expected"
                    ],
                    f"{prefix}_large_confirmation_loss": row["large_confirmation_loss"],
                }
            )

        rows.append(wide)

    return sorted(
        rows,
        key=lambda row: (
            Decimal(row["total_lost_vs_full_weight_gnk"]),
            row["address"],
        ),
        reverse=True,
    )


def metric_wide_rows(
    long_rows: list[dict[str, Any]],
    from_epoch: int,
    to_epoch: int,
    metric_prefix: str,
    value_by_row: Any,
) -> list[dict[str, Any]]:
    by_address: dict[str, dict[int, dict[str, Any]]] = {}
    for row in long_rows:
        by_address.setdefault(row["address"], {})[int(row["epoch"])] = row

    rows: list[dict[str, Any]] = []
    for address, by_epoch in by_address.items():
        wide: dict[str, Any] = {"address": address}
        total = Decimal(0)
        for epoch in range(from_epoch, to_epoch + 1):
            row = by_epoch.get(epoch)
            field = f"epoch_{epoch}_{metric_prefix}_gnk"
            if row is None:
                wide[field] = ""
                continue
            value = Decimal(str(value_by_row(row)))
            total += value
            wide[field] = blank_zero_decimal(value)
        wide[f"total_{metric_prefix}_gnk"] = blank_zero_decimal(total)
        rows.append(wide)

    return sorted(
        rows,
        key=lambda row: (
            Decimal(row[f"total_{metric_prefix}_gnk"] or "0"),
            row["address"],
        ),
        reverse=True,
    )


def weight_wide_rows(
    long_rows: list[dict[str, Any]],
    from_epoch: int,
    to_epoch: int,
    metric_prefix: str,
    value_by_row: Any,
) -> list[dict[str, Any]]:
    by_address: dict[str, dict[int, dict[str, Any]]] = {}
    for row in long_rows:
        by_address.setdefault(row["address"], {})[int(row["epoch"])] = row

    rows: list[dict[str, Any]] = []
    for address, by_epoch in by_address.items():
        wide: dict[str, Any] = {"address": address}
        total = 0
        for epoch in range(from_epoch, to_epoch + 1):
            row = by_epoch.get(epoch)
            field = f"epoch_{epoch}_{metric_prefix}"
            if row is None:
                wide[field] = ""
                continue
            value = int(value_by_row(row))
            total += value
            wide[field] = "" if value == 0 else value
        wide[f"total_{metric_prefix}"] = "" if total == 0 else total
        rows.append(wide)

    return sorted(
        rows,
        key=lambda row: (int(row[f"total_{metric_prefix}"] or 0), row["address"]),
        reverse=True,
    )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise RuntimeError("no rows to write")
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-url", default=DEFAULT_NODE_URL)
    parser.add_argument("--from-epoch", type=int, default=DEFAULT_FROM_EPOCH)
    parser.add_argument("--to-epoch", type=int, default=DEFAULT_TO_EPOCH)
    parser.add_argument(
        "--confirmation-reward-from-epoch",
        type=int,
        default=DEFAULT_CONFIRMATION_REWARD_FROM_EPOCH,
    )
    parser.add_argument(
        "--stuck-weight-baseline-epoch",
        type=int,
        default=DEFAULT_STUCK_WEIGHT_BASELINE_EPOCH,
    )
    parser.add_argument(
        "--stuck-weight-from-epoch",
        type=int,
        default=DEFAULT_STUCK_WEIGHT_FROM_EPOCH,
    )
    parser.add_argument("--large-loss-threshold", default=str(DEFAULT_LARGE_LOSS_THRESHOLD))
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--confirmation-plus-poc-slot-minus-effective-output",
        default=DEFAULT_CONFIRMATION_PLUS_POC_SLOT_MINUS_EFFECTIVE_OUTPUT,
    )
    parser.add_argument("--chain-delta-output", default=DEFAULT_CHAIN_DELTA_OUTPUT)
    parser.add_argument(
        "--035-bug-fix-delta-output",
        dest="bug_fix_035_delta_output",
        default=DEFAULT_035_BUG_FIX_DELTA_OUTPUT,
    )
    parser.add_argument(
        "--035-bug-fix-effective-weight-delta-output",
        dest="bug_fix_035_effective_weight_delta_output",
        default=DEFAULT_035_BUG_FIX_EFFECTIVE_WEIGHT_DELTA_OUTPUT,
    )
    parser.add_argument(
        "--inference-slot-weight-output",
        default=DEFAULT_INFERENCE_SLOT_WEIGHT_OUTPUT,
    )
    parser.add_argument(
        "--preserved-event-weight-output",
        default=DEFAULT_PRESERVED_EVENT_WEIGHT_OUTPUT,
    )
    parser.add_argument(
        "--long-output",
        help="optional path for the detailed participant-epoch table",
    )
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--retries", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    long_rows = calculate(args)
    rows = wide_rows(long_rows, args.from_epoch, args.to_epoch)
    write_csv(Path(args.output), rows)
    confirmation_plus_poc_slot_minus_effective_rows = metric_wide_rows(
        long_rows,
        args.from_epoch,
        args.to_epoch,
        "confirmation_plus_poc_slot_minus_effective_reward",
        lambda row: Decimal(str(row["expected_confirmation_plus_poc_slot_reward_gnk"]))
        - Decimal(str(row["expected_effective_reward_gnk"])),
    )
    write_csv(
        Path(args.confirmation_plus_poc_slot_minus_effective_output),
        confirmation_plus_poc_slot_minus_effective_rows,
    )
    chain_delta_rows = metric_wide_rows(
        long_rows,
        args.from_epoch,
        args.to_epoch,
        "chain_expected_delta",
        lambda row: Decimal(str(row["chain_expected_delta_gnk"])),
    )
    write_csv(Path(args.chain_delta_output), chain_delta_rows)
    bug_fix_delta_rows = metric_wide_rows(
        long_rows,
        args.from_epoch,
        args.to_epoch,
        "035_bug_fix_expected_minus_actual",
        lambda row: Decimal(str(row["expected_035_bug_fix_weight_reward_gnk"]))
        - Decimal(str(row["actual_reward_gnk"])),
    )
    write_csv(Path(args.bug_fix_035_delta_output), bug_fix_delta_rows)
    bug_fix_effective_weight_delta_rows = weight_wide_rows(
        long_rows,
        args.from_epoch,
        args.to_epoch,
        "035_bug_fix_weight_minus_effective_weight",
        lambda row: int(row["weight_with_035_bug_fix"]) - int(row["effective_weight"]),
    )
    write_csv(
        Path(args.bug_fix_035_effective_weight_delta_output),
        bug_fix_effective_weight_delta_rows,
    )
    inference_slot_weight_rows = weight_wide_rows(
        long_rows,
        args.from_epoch,
        args.to_epoch,
        "inference_slot_weight",
        lambda row: row["poc_slot_weight"],
    )
    write_csv(Path(args.inference_slot_weight_output), inference_slot_weight_rows)
    preserved_event_weight_rows = preserved_event_weight_wide_rows(args, long_rows)
    write_csv(Path(args.preserved_event_weight_output), preserved_event_weight_rows)
    if args.long_output:
        write_csv(Path(args.long_output), long_rows)
    zero_paid = sum(1 for row in long_rows if row["zero_paid_with_positive_expected"])
    large_loss = sum(1 for row in long_rows if row["large_confirmation_loss"])
    mismatches = sum(1 for row in long_rows if not row["chain_reward_matches_actual"])
    print(f"wrote {args.output}")
    print(f"wrote {args.confirmation_plus_poc_slot_minus_effective_output}")
    print(f"wrote {args.chain_delta_output}")
    print(f"wrote {args.bug_fix_035_delta_output}")
    print(f"wrote {args.bug_fix_035_effective_weight_delta_output}")
    print(f"wrote {args.inference_slot_weight_output}")
    print(f"wrote {args.preserved_event_weight_output}")
    if args.long_output:
        print(f"wrote {args.long_output}")
    print(f"participant rows: {len(rows)}")
    print(f"participant-epoch rows: {len(long_rows)}")
    print(f"zero_paid_with_positive_expected: {zero_paid}")
    print(f"large_confirmation_loss: {large_loss}")
    print(f"chain_reward_mismatches: {mismatches}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
