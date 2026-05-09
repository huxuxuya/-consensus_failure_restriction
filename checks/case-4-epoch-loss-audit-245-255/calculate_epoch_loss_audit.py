#!/usr/bin/env python3
"""Generate broad epoch loss audit table for Gonka epochs 245-255."""

from __future__ import annotations

import argparse
import csv
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
DEFAULT_CONFIRMATION_MINUS_EFFECTIVE_OUTPUT = (
    "checks/case-4-epoch-loss-audit-245-255/"
    "confirmation_minus_effective_reward_245_255.csv"
)
DEFAULT_CHAIN_DELTA_OUTPUT = (
    "checks/case-4-epoch-loss-audit-245-255/chain_expected_delta_245_255.csv"
)
DEFAULT_LARGE_LOSS_THRESHOLD = Decimal("0.5")
DEFAULT_CONFIRMATION_REWARD_FROM_EPOCH = 248
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
            confirmation_weight = max(0, to_int(vw.get("confirmation_weight")))
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
                    "confirmation_weight": confirmation_weight,
                    "effective_weight": effective_weight,
                    "poc_slot_weight": poc_slot_weight,
                    "preserved_poc_weight": poc_slot_weight,
                    "excluded": address in excluded_addresses,
                    "fixed_epoch_reward_gnk": to_gnk(fixed_epoch_reward),
                    "total_epoch_weight": total_epoch_weight,
                    "actual_reward_gnk": to_gnk(actual_reward),
                    "expected_full_weight_reward_gnk": to_gnk(expected_full),
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


def sum_decimal_strings(rows: list[dict[str, Any]], field: str) -> Decimal:
    return sum((Decimal(str(row[field])) for row in rows), Decimal(0))


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
                        f"{prefix}_confirmation_weight": "",
                        f"{prefix}_effective_weight": "",
                        f"{prefix}_poc_slot_weight": "",
                        f"{prefix}_excluded": "",
                        f"{prefix}_actual_reward_gnk": "",
                        f"{prefix}_expected_full_weight_reward_gnk": "",
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
                    f"{prefix}_confirmation_weight": row["confirmation_weight"],
                    f"{prefix}_effective_weight": row["effective_weight"],
                    f"{prefix}_poc_slot_weight": row["poc_slot_weight"],
                    f"{prefix}_excluded": row["excluded"],
                    f"{prefix}_actual_reward_gnk": row["actual_reward_gnk"],
                    f"{prefix}_expected_full_weight_reward_gnk": row[
                        "expected_full_weight_reward_gnk"
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
            value = value_by_row(row)
            total += Decimal(str(value))
            wide[field] = str(value)
        wide[f"total_{metric_prefix}_gnk"] = str(total)
        rows.append(wide)

    return sorted(
        rows,
        key=lambda row: (Decimal(row[f"total_{metric_prefix}_gnk"]), row["address"]),
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
    parser.add_argument("--large-loss-threshold", default=str(DEFAULT_LARGE_LOSS_THRESHOLD))
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--confirmation-minus-effective-output",
        default=DEFAULT_CONFIRMATION_MINUS_EFFECTIVE_OUTPUT,
    )
    parser.add_argument("--chain-delta-output", default=DEFAULT_CHAIN_DELTA_OUTPUT)
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
    confirmation_minus_effective_rows = metric_wide_rows(
        long_rows,
        args.from_epoch,
        args.to_epoch,
        "confirmation_minus_effective_reward",
        lambda row: Decimal(str(row["expected_confirmation_weight_reward_gnk"]))
        - Decimal(str(row["expected_effective_reward_gnk"])),
    )
    write_csv(
        Path(args.confirmation_minus_effective_output),
        confirmation_minus_effective_rows,
    )
    chain_delta_rows = metric_wide_rows(
        long_rows,
        args.from_epoch,
        args.to_epoch,
        "chain_expected_delta",
        lambda row: Decimal(str(row["chain_expected_delta_gnk"])),
    )
    write_csv(Path(args.chain_delta_output), chain_delta_rows)
    if args.long_output:
        write_csv(Path(args.long_output), long_rows)
    zero_paid = sum(1 for row in long_rows if row["zero_paid_with_positive_expected"])
    large_loss = sum(1 for row in long_rows if row["large_confirmation_loss"])
    mismatches = sum(1 for row in long_rows if not row["chain_reward_matches_actual"])
    print(f"wrote {args.output}")
    print(f"wrote {args.confirmation_minus_effective_output}")
    print(f"wrote {args.chain_delta_output}")
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
