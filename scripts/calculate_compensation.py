#!/usr/bin/env python3
"""Discover affected Gonka participants and export compensation calculation CSVs."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_FLOOR, getcontext
from pathlib import Path
from typing import Any


getcontext().prec = 50

DEFAULT_NODE_URL = "http://node1.gonka.ai:8000"
DEFAULT_SCAN_FROM_EPOCH = 220
DEFAULT_EXCLUDE_FROM_EPOCH = 250
DEFAULT_OUTPUT = "artifacts/compensation_calculation.csv"
DEFAULT_AFFECTED_OUTPUT = "artifacts/affected_participants.csv"
DEFAULT_AUDIT_OUTPUT = "artifacts/paid_then_unpaid_audit.csv"
DEFAULT_INVALID_STATUS_OUTPUT = "artifacts/invalid_status_by_epoch.csv"
DEFAULT_CONFIRMATION_REWARD_FROM_EPOCH = 248
INVALID_EXCLUSION_REASONS = {"consecutive_failures", "statistical_invalidations"}
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


def fetch_json(url: str, timeout: int, retries: int) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}: {last_error}") from last_error


def endpoint(node_url: str, path: str) -> str:
    return node_url.rstrip("/") + path


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
        batch = payload.get(item_key, [])
        if not isinstance(batch, list):
            raise RuntimeError(f"expected list at key {item_key!r} from {url}")
        items.extend(batch)

        next_key = payload.get("pagination", {}).get("next_key")
        if not next_key:
            return items

        query = urllib.parse.urlencode({"pagination.key": next_key})
        url = endpoint(node_url, path) + "?" + query


def to_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    return int(value)


def decimal_floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def unique_addresses(addresses: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for address in addresses:
        if address not in seen:
            seen.add(address)
            result.append(address)
    return result


def participant_map(participants: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for participant in participants:
        address = participant.get("address") or participant.get("index")
        if address:
            result[address] = participant
    return result


def load_epoch_group(node_url: str, epoch: int, timeout: int, retries: int) -> dict[str, Any]:
    path = f"/chain-api/productscience/inference/inference/epoch_group_data/{epoch}"
    payload = fetch_json(endpoint(node_url, path), timeout=timeout, retries=retries)
    group = payload.get("epoch_group_data")
    if not isinstance(group, dict):
        raise RuntimeError(f"missing epoch_group_data for epoch {epoch}")
    return group


def load_epoch_groups_by_epoch(
    node_url: str,
    epochs: set[int],
    timeout: int,
    retries: int,
) -> dict[int, list[dict[str, Any]]]:
    if not epochs:
        return {}
    groups = fetch_paginated(
        node_url,
        "/chain-api/productscience/inference/inference/epoch_group_data",
        "epoch_group_data",
        timeout=timeout,
        retries=retries,
    )
    by_epoch: dict[int, list[dict[str, Any]]] = {}
    for group in groups:
        epoch = to_int(group.get("epoch_index"))
        if epoch in epochs:
            by_epoch.setdefault(epoch, []).append(group)
    return by_epoch


def load_excluded_addresses(
    node_url: str,
    epoch: int,
    timeout: int,
    retries: int,
) -> set[str]:
    payload = fetch_json(
        endpoint(
            node_url,
            f"/chain-api/productscience/inference/inference/excluded_participants/{epoch}",
        ),
        timeout=timeout,
        retries=retries,
    )
    return {item["address"] for item in payload.get("items", []) if item.get("address")}


def load_exclusion_reasons(
    node_url: str,
    epoch: int,
    timeout: int,
    retries: int,
) -> dict[str, str]:
    payload = fetch_json(
        endpoint(
            node_url,
            f"/chain-api/productscience/inference/inference/excluded_participants/{epoch}",
        ),
        timeout=timeout,
        retries=retries,
    )
    return {
        item["address"]: item.get("reason", "excluded")
        for item in payload.get("items", [])
        if item.get("address")
    }


def load_epoch_performance(
    node_url: str,
    epoch: int,
    timeout: int,
    retries: int,
) -> list[dict[str, Any]]:
    path = f"/chain-api/productscience/inference/inference/epoch_performance_summary/{epoch}"
    payload = fetch_json(endpoint(node_url, path), timeout=timeout, retries=retries)
    summary = payload.get("epochPerformanceSummary", [])
    if not isinstance(summary, list):
        raise RuntimeError(f"missing epochPerformanceSummary for epoch {epoch}")
    return summary


def load_params(node_url: str, timeout: int, retries: int) -> dict[str, Any]:
    payload = fetch_json(
        endpoint(node_url, "/chain-api/productscience/inference/inference/params"),
        timeout=timeout,
        retries=retries,
    )
    params = payload.get("params")
    if not isinstance(params, dict):
        raise RuntimeError("missing params")
    return params


def parse_chain_decimal(value: dict[str, Any]) -> Decimal:
    return Decimal(str(value["value"])) * (Decimal(10) ** int(value["exponent"]))


def calculate_fixed_epoch_reward(
    epoch: int,
    bitcoin_reward_params: dict[str, Any],
) -> int:
    initial_reward = Decimal(str(bitcoin_reward_params["initial_epoch_reward"]))
    genesis_epoch = int(bitcoin_reward_params["genesis_epoch"])
    decay_rate = parse_chain_decimal(bitcoin_reward_params["decay_rate"])
    epochs_since_genesis = epoch - genesis_epoch
    if epochs_since_genesis <= 0:
        return int(initial_reward)

    exponent = DECAY_EXPONENTS.get(decay_rate)
    if exponent is None:
        raise RuntimeError(f"unsupported decay rate: {decay_rate}")

    return decimal_floor(initial_reward * (exponent ** epochs_since_genesis))


def extract_weights(epoch_group: dict[str, Any]) -> dict[str, dict[str, int]]:
    weights: dict[str, dict[str, int]] = {}
    for item in epoch_group.get("validation_weights", []):
        address = item.get("member_address")
        if not address:
            continue
        weight = to_int(item.get("weight"))
        confirmation_weight = to_int(item.get("confirmation_weight"))
        weights[address] = {
            "weight": weight,
            "confirmation_weight": confirmation_weight,
            "effective_weight": min(weight, confirmation_weight),
        }
    return weights


def preserved_poc_weight(validation_weight: dict[str, Any]) -> int:
    total = 0
    for node in validation_weight.get("ml_nodes", []):
        slots = node.get("timeslot_allocation") or []
        if len(slots) > 1 and bool(slots[1]):
            total += to_int(node.get("poc_weight"))
    return total


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


def dynamic_p0_permille(
    performance: list[dict[str, Any]],
    governance_permille: int,
) -> tuple[int, bool]:
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
    return max(governance_permille, selected), selected == 500


def missed_stat_test_passed(missed: int, total: int, p0_permille: int) -> bool:
    if total == 0:
        return True
    if missed < 0 or total < 0 or missed > total:
        return False
    if total > MAX_TABLE_N:
        numerator, denominator = P0_MULTIPLIERS[p0_permille]
        return missed * denominator <= total * numerator

    p0 = p0_permille / 1000
    critical = math.floor(
        total * p0 + 1.6448536269514722 * math.sqrt(total * p0 * (1 - p0))
    )
    return missed <= min(critical, total)


def apply_power_capping(weights: dict[str, int]) -> dict[str, int]:
    positive = [(address, weight) for address, weight in weights.items() if weight > 0]
    if len(positive) <= 1:
        return weights.copy()

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


def calculate_chain_reward_weights(
    epoch: int,
    root_group: dict[str, Any],
    model_groups: list[dict[str, Any]],
    coefficients: dict[str, Decimal],
    performance: list[dict[str, Any]],
    excluded_addresses: set[str],
    restored_address: str,
    governance_p0_permille: int,
    confirmation_reward_from_epoch: int,
) -> dict[str, int]:
    preserved_by_address: dict[str, int] = {}
    raw_totals: dict[str, int] = {}
    for group in model_groups:
        model_id = group.get("model_id", "")
        for vw in group.get("validation_weights", []):
            address = vw["member_address"]
            preserved_by_address[address] = (
                preserved_by_address.get(address, 0) + preserved_poc_weight(vw)
            )
            raw_totals[address] = raw_totals.get(address, 0) + coefficient_adjusted_weight(
                vw,
                model_id,
                coefficients,
            )

    effective_weights: dict[str, int] = {}
    for vw in root_group.get("validation_weights", []):
        address = vw["member_address"]
        weight = max(0, to_int(vw.get("weight")))
        if weight <= 0 or (address in excluded_addresses and address != restored_address):
            effective_weights[address] = 0
            continue

        confirmation_weight = max(0, to_int(vw.get("confirmation_weight")))
        if epoch < confirmation_reward_from_epoch:
            effective_weight = min(
                weight,
                confirmation_weight + preserved_by_address.get(address, 0),
            )
        else:
            raw_total = raw_totals.get(address, 0)
            if raw_total > 0 and weight < raw_total:
                effective_weight = confirmation_weight * weight // raw_total
            else:
                effective_weight = confirmation_weight
            effective_weight = min(weight, max(0, effective_weight))
        effective_weights[address] = effective_weight

    participant_weights = apply_power_capping(effective_weights)
    p0_permille, skip_punishment = dynamic_p0_permille(performance, governance_p0_permille)
    performance_by_address = performance_map(performance)
    if not skip_punishment:
        for address in list(participant_weights.keys()):
            stats = performance_by_address.get(address, {})
            total = to_int(stats.get("inference_count")) + to_int(stats.get("missed_requests"))
            missed = to_int(stats.get("missed_requests"))
            if not missed_stat_test_passed(missed, total, p0_permille):
                participant_weights[address] = 0

    return participant_weights


def performance_map(summary: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in summary:
        participant_id = item.get("participant_id")
        if participant_id:
            result[participant_id] = item
    return result


def discover_invalid_participants(
    participants: dict[str, dict[str, Any]],
    explicit_addresses: list[str] | None,
) -> list[str]:
    if explicit_addresses:
        return unique_addresses(explicit_addresses)

    return sorted(
        address
        for address, participant in participants.items()
        if participant.get("status") == "INVALID"
    )


def find_lost_epoch(
    address: str,
    performance_by_epoch: dict[int, dict[str, dict[str, Any]]],
    scan_from_epoch: int,
    exclude_from_epoch: int,
) -> tuple[int | None, int | None]:
    paid_epochs: list[int] = []
    participant_epochs: list[int] = []

    for epoch in range(scan_from_epoch, exclude_from_epoch):
        performance = performance_by_epoch.get(epoch, {}).get(address)
        if not performance:
            continue
        participant_epochs.append(epoch)
        if to_int(performance.get("rewarded_coins")) > 0:
            paid_epochs.append(epoch)

    if not paid_epochs:
        return None, None

    last_paid_epoch = max(paid_epochs)
    for epoch in participant_epochs:
        if epoch > last_paid_epoch:
            performance = performance_by_epoch[epoch][address]
            if to_int(performance.get("rewarded_coins")) == 0:
                return last_paid_epoch, epoch

    return last_paid_epoch, None


def build_affected_rows(
    addresses: list[str],
    participants: dict[str, dict[str, Any]],
    performance_by_epoch: dict[int, dict[str, dict[str, Any]]],
    scan_from_epoch: int,
    exclude_from_epoch: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    for address in addresses:
        participant = participants.get(address, {})
        last_paid_epoch, first_unpaid_epoch = find_lost_epoch(
            address,
            performance_by_epoch,
            scan_from_epoch,
            exclude_from_epoch,
        )
        performance_epochs = [
            epoch
            for epoch in range(scan_from_epoch, exclude_from_epoch)
            if address in performance_by_epoch.get(epoch, {})
        ]

        rows.append(
            {
                "address": address,
                "current_status": participant.get("status", ""),
                "consecutive_invalid_inferences": participant.get(
                    "consecutive_invalid_inferences",
                    "",
                ),
                "join_height": participant.get("join_height", ""),
                "epochs_completed": participant.get("epochs_completed", ""),
                "scan_from_epoch": scan_from_epoch,
                "exclude_from_epoch_policy": exclude_from_epoch,
                "first_seen_performance_epoch": min(performance_epochs)
                if performance_epochs
                else "",
                "last_seen_performance_epoch": max(performance_epochs)
                if performance_epochs
                else "",
                "last_paid_epoch": last_paid_epoch if last_paid_epoch is not None else "",
                "first_unpaid_epoch_after_last_paid": first_unpaid_epoch
                if first_unpaid_epoch is not None
                else "",
                "included_for_compensation": bool(first_unpaid_epoch is not None),
            }
        )

    return rows


def build_paid_then_unpaid_audit_rows(
    participants: dict[str, dict[str, Any]],
    performance_by_epoch: dict[int, dict[str, dict[str, Any]]],
    scan_from_epoch: int,
    exclude_from_epoch: int,
) -> list[dict[str, Any]]:
    addresses = sorted(
        {
            address
            for by_address in performance_by_epoch.values()
            for address in by_address.keys()
        }
    )
    rows: list[dict[str, Any]] = []

    for address in addresses:
        performance_epochs = [
            epoch
            for epoch in range(scan_from_epoch, exclude_from_epoch)
            if address in performance_by_epoch.get(epoch, {})
        ]
        if not performance_epochs:
            continue

        paid_epochs = [
            epoch
            for epoch in performance_epochs
            if to_int(performance_by_epoch[epoch][address].get("rewarded_coins")) > 0
        ]
        if not paid_epochs:
            continue

        last_paid_epoch = max(paid_epochs)
        unpaid_after_last_paid = [
            epoch
            for epoch in performance_epochs
            if epoch > last_paid_epoch
            and to_int(performance_by_epoch[epoch][address].get("rewarded_coins")) == 0
        ]
        if not unpaid_after_last_paid:
            continue

        participant = participants.get(address, {})
        rows.append(
            {
                "address": address,
                "current_status": participant.get("status", "MISSING"),
                "consecutive_invalid_inferences": participant.get(
                    "consecutive_invalid_inferences",
                    "",
                ),
                "first_seen_performance_epoch": min(performance_epochs),
                "last_seen_performance_epoch": max(performance_epochs),
                "last_paid_epoch": last_paid_epoch,
                "first_unpaid_epoch_after_last_paid": min(unpaid_after_last_paid),
                "matches_strict_bug_filter": participant.get("status") == "INVALID",
            }
        )

    return sorted(
        rows,
        key=lambda row: (
            int(row["first_unpaid_epoch_after_last_paid"]),
            str(row["address"]),
        ),
    )


def build_invalid_status_wide_rows(
    participants: dict[str, dict[str, Any]],
    performance_by_epoch: dict[int, dict[str, dict[str, Any]]],
    exclusion_reasons_by_epoch: dict[int, dict[str, str]],
    scan_from_epoch: int,
    exclude_from_epoch: int,
) -> list[dict[str, Any]]:
    addresses = sorted(
        {
            address
            for by_address in performance_by_epoch.values()
            for address in by_address.keys()
        }
    )
    rows: list[dict[str, Any]] = []

    for address in addresses:
        wide: dict[str, Any] = {
            "address": address,
            "current_status": participants.get(address, {}).get("status", ""),
        }
        has_invalid_epoch = False
        for epoch in range(scan_from_epoch, exclude_from_epoch):
            reason = exclusion_reasons_by_epoch.get(epoch, {}).get(address, "")
            value = "INVALID" if reason in INVALID_EXCLUSION_REASONS else ""
            has_invalid_epoch = has_invalid_epoch or bool(value)
            wide[f"epoch_{epoch}"] = value

        rows.append(wide)

    return rows


def calculate_reward_rate(
    fixed_epoch_reward: int,
    total_epoch_weight: int,
) -> Decimal:
    if total_epoch_weight <= 0:
        raise RuntimeError("cannot calculate reward rate: total epoch weight is zero")
    return Decimal(fixed_epoch_reward) / Decimal(total_epoch_weight)


def build_rows(
    affected_rows: list[dict[str, Any]],
    participants: dict[str, dict[str, Any]],
    weights_by_epoch: dict[int, dict[str, dict[str, int]]],
    performance_by_epoch: dict[int, dict[str, dict[str, Any]]],
    exclude_from_epoch: int,
    reward_rates_by_epoch: dict[int, tuple[int, int, Decimal]],
    chain_reward_weights_by_epoch: dict[int, dict[str, int]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    for affected in affected_rows:
        address = str(affected["address"])
        lost_epoch_value = affected["first_unpaid_epoch_after_last_paid"]
        if lost_epoch_value == "":
            continue
        lost_epoch = int(lost_epoch_value)
        if lost_epoch >= exclude_from_epoch:
            continue

        participant = participants.get(address, {})
        weights_by_address = weights_by_epoch.get(lost_epoch, {})
        weight_info = weights_by_address.get(
            address,
            {"weight": 0, "confirmation_weight": 0, "effective_weight": 0},
        )
        performance = performance_by_epoch.get(lost_epoch, {}).get(address, {})
        fixed_epoch_reward, total_epoch_weight, reward_rate = reward_rates_by_epoch[
            lost_epoch
        ]

        effective_weight = chain_reward_weights_by_epoch.get(lost_epoch, {}).get(
            address,
            weight_info["effective_weight"],
        )
        expected_reward = decimal_floor(Decimal(effective_weight) * reward_rate)
        actual_reward = to_int(performance.get("rewarded_coins"))
        compensation = max(0, expected_reward - actual_reward)

        rows.append(
            {
                "address": address,
                "lost_epoch": lost_epoch,
                "excluded_from_epoch_policy": exclude_from_epoch,
                "last_paid_epoch": affected["last_paid_epoch"],
                "current_status": participant.get("status", ""),
                "consecutive_invalid_inferences": participant.get(
                    "consecutive_invalid_inferences",
                    "",
                ),
                "join_height": participant.get("join_height", ""),
                "epochs_completed": participant.get("epochs_completed", ""),
                "weight": weight_info["weight"],
                "confirmation_weight": weight_info["confirmation_weight"],
                "effective_weight": effective_weight,
                "fixed_epoch_reward_for_rate": fixed_epoch_reward,
                "total_epoch_weight_for_rate": total_epoch_weight,
                "reward_rate_base_units_per_weight": str(reward_rate),
                "inference_count": to_int(performance.get("inference_count")),
                "missed_requests": to_int(performance.get("missed_requests")),
                "earned_coins": to_int(performance.get("earned_coins")),
                "actual_rewarded_coins": actual_reward,
                "burned_coins": to_int(performance.get("burned_coins")),
                "validated_inferences": to_int(performance.get("validated_inferences")),
                "invalidated_inferences": to_int(performance.get("invalidated_inferences")),
                "claimed": performance.get("claimed", ""),
                "expected_reward_base_units": expected_reward,
                "compensation_base_units": compensation,
                "compensation_gnk": str(Decimal(compensation) / Decimal(1_000_000_000)),
            }
        )

    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise RuntimeError("no rows to write")

    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover Gonka affected participants and calculate compensation CSVs.",
    )
    parser.add_argument("--node-url", default=DEFAULT_NODE_URL)
    parser.add_argument("--scan-from-epoch", type=int, default=DEFAULT_SCAN_FROM_EPOCH)
    parser.add_argument(
        "--exclude-from-epoch",
        type=int,
        default=DEFAULT_EXCLUDE_FROM_EPOCH,
        help="Documented policy cutoff. Epochs >= this value are not compensated.",
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--affected-output", default=DEFAULT_AFFECTED_OUTPUT)
    parser.add_argument("--audit-output", default=DEFAULT_AUDIT_OUTPUT)
    parser.add_argument("--invalid-status-output", default=DEFAULT_INVALID_STATUS_OUTPUT)
    parser.add_argument(
        "--confirmation-reward-from-epoch",
        type=int,
        default=DEFAULT_CONFIRMATION_REWARD_FROM_EPOCH,
    )
    parser.add_argument(
        "--address",
        action="append",
        dest="addresses",
        help=(
            "Optional affected address override. Can be passed multiple times. "
            "If omitted, the script discovers all current INVALID participants."
        ),
    )
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--retries", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.scan_from_epoch >= args.exclude_from_epoch:
        print(
            f"error: scan start {args.scan_from_epoch} is not before policy cutoff "
            f"{args.exclude_from_epoch}",
            file=sys.stderr,
        )
        return 2

    print("stage 1/4: fetching current participants", flush=True)
    participants = fetch_paginated(
        args.node_url,
        "/chain-api/productscience/inference/inference/participant",
        "participant",
        timeout=args.timeout,
        retries=args.retries,
    )
    participants_by_address = participant_map(participants)
    addresses = discover_invalid_participants(participants_by_address, args.addresses)
    print(f"stage 1/4: found {len(addresses)} affected candidate(s)", flush=True)

    params = load_params(args.node_url, timeout=args.timeout, retries=args.retries)
    bitcoin_reward_params = params["bitcoin_reward_params"]
    coefficients = model_coefficients(params)
    validation_params = params.get("validation_params", {})
    governance_p0 = parse_chain_decimal(
        validation_params.get("binom_test_p0", {"value": "1", "exponent": -1})
    )
    governance_p0_permille = ceil_supported_p0_permille(
        int((governance_p0 * Decimal(1000)).to_integral_value(rounding=ROUND_FLOOR))
    )

    print(
        "stage 2/4: fetching epoch performance summaries and exclusions "
        f"{args.scan_from_epoch}-{args.exclude_from_epoch - 1}",
        flush=True,
    )
    performance_by_epoch: dict[int, dict[str, dict[str, Any]]] = {}
    exclusion_reasons_by_epoch: dict[int, dict[str, str]] = {}
    for epoch in range(args.scan_from_epoch, args.exclude_from_epoch):
        performance = load_epoch_performance(
            args.node_url,
            epoch,
            timeout=args.timeout,
            retries=args.retries,
        )
        performance_by_epoch[epoch] = performance_map(performance)
        exclusion_reasons_by_epoch[epoch] = load_exclusion_reasons(
            args.node_url,
            epoch,
            timeout=args.timeout,
            retries=args.retries,
        )

    affected_rows = build_affected_rows(
        addresses=addresses,
        participants=participants_by_address,
        performance_by_epoch=performance_by_epoch,
        scan_from_epoch=args.scan_from_epoch,
        exclude_from_epoch=args.exclude_from_epoch,
    )
    audit_rows = build_paid_then_unpaid_audit_rows(
        participants=participants_by_address,
        performance_by_epoch=performance_by_epoch,
        scan_from_epoch=args.scan_from_epoch,
        exclude_from_epoch=args.exclude_from_epoch,
    )
    invalid_status_rows = build_invalid_status_wide_rows(
        participants=participants_by_address,
        performance_by_epoch=performance_by_epoch,
        exclusion_reasons_by_epoch=exclusion_reasons_by_epoch,
        scan_from_epoch=args.scan_from_epoch,
        exclude_from_epoch=args.exclude_from_epoch,
    )
    write_csv(Path(args.affected_output), affected_rows)
    write_csv(Path(args.audit_output), audit_rows)
    write_csv(Path(args.invalid_status_output), invalid_status_rows)
    print(f"stage 2/4: wrote discovery CSV: {args.affected_output}", flush=True)
    print(f"stage 2/4: wrote broad audit CSV: {args.audit_output}", flush=True)
    print(f"stage 2/4: wrote invalid status CSV: {args.invalid_status_output}", flush=True)

    lost_epochs = sorted(
        {
            int(row["first_unpaid_epoch_after_last_paid"])
            for row in affected_rows
            if row["first_unpaid_epoch_after_last_paid"] != ""
        }
    )

    print(
        f"stage 3/4: fetching epoch weights for lost epochs: {lost_epochs}",
        flush=True,
    )
    epoch_groups_by_epoch = load_epoch_groups_by_epoch(
        args.node_url,
        set(lost_epochs),
        timeout=args.timeout,
        retries=args.retries,
    )
    weights_by_epoch: dict[int, dict[str, dict[str, int]]] = {}
    reward_rates_by_epoch: dict[int, tuple[int, int, Decimal]] = {}
    chain_reward_weights_by_epoch: dict[int, dict[str, int]] = {}
    for epoch in lost_epochs:
        epoch_groups = epoch_groups_by_epoch.get(epoch, [])
        epoch_group = next(
            (group for group in epoch_groups if group.get("model_id", "") == ""),
            None,
        )
        if epoch_group is None:
            epoch_group = load_epoch_group(
                args.node_url,
                epoch,
                timeout=args.timeout,
                retries=args.retries,
            )
        model_groups = [group for group in epoch_groups if group.get("model_id", "") != ""]
        weights_by_epoch[epoch] = extract_weights(epoch_group)
        fixed_epoch_reward = calculate_fixed_epoch_reward(
            epoch,
            bitcoin_reward_params,
        )
        total_epoch_weight = to_int(epoch_group.get("total_weight"))
        reward_rates_by_epoch[epoch] = (
            fixed_epoch_reward,
            total_epoch_weight,
            calculate_reward_rate(fixed_epoch_reward, total_epoch_weight),
        )
        performance = list(performance_by_epoch.get(epoch, {}).values())
        excluded_addresses = load_excluded_addresses(
            args.node_url,
            epoch,
            timeout=args.timeout,
            retries=args.retries,
        )
        for row in affected_rows:
            if row["first_unpaid_epoch_after_last_paid"] == epoch:
                address = str(row["address"])
                per_address_weights = calculate_chain_reward_weights(
                    epoch=epoch,
                    root_group=epoch_group,
                    model_groups=model_groups,
                    coefficients=coefficients,
                    performance=performance,
                    excluded_addresses=excluded_addresses,
                    restored_address=address,
                    governance_p0_permille=governance_p0_permille,
                    confirmation_reward_from_epoch=args.confirmation_reward_from_epoch,
                )
                chain_reward_weights_by_epoch.setdefault(epoch, {})[address] = (
                    per_address_weights.get(address, 0)
                )

    print("stage 4/4: calculating compensation", flush=True)
    compensation_rows = build_rows(
        affected_rows=affected_rows,
        participants=participants_by_address,
        weights_by_epoch=weights_by_epoch,
        performance_by_epoch=performance_by_epoch,
        exclude_from_epoch=args.exclude_from_epoch,
        reward_rates_by_epoch=reward_rates_by_epoch,
        chain_reward_weights_by_epoch=chain_reward_weights_by_epoch,
    )
    write_csv(Path(args.output), compensation_rows)

    total = sum(to_int(row["compensation_base_units"]) for row in compensation_rows)
    print(f"wrote {args.affected_output}", flush=True)
    print(f"wrote {args.audit_output}", flush=True)
    print(f"wrote {args.invalid_status_output}", flush=True)
    print(f"wrote {args.output}", flush=True)
    print(f"affected_candidates: {len(affected_rows)}", flush=True)
    print(f"paid_then_unpaid_audit_rows: {len(audit_rows)}", flush=True)
    print(f"invalid_status_rows: {len(invalid_status_rows)}", flush=True)
    print(f"compensation_rows: {len(compensation_rows)}", flush=True)
    print(f"total_compensation_base_units: {total}", flush=True)
    print(
        f"total_compensation_gnk: {Decimal(total) / Decimal(1_000_000_000)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
