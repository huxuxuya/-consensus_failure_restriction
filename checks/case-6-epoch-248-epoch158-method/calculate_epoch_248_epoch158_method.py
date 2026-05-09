#!/usr/bin/env python3
"""Run epoch158-style historical POC_SLOT loss check for epoch 248."""

from __future__ import annotations

import argparse
import csv
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, getcontext
from pathlib import Path
from typing import Any


getcontext().prec = 80

DEFAULT_NODE_URL = "http://node1.gonka.ai:8000"
DEFAULT_EPOCH = 248
DEFAULT_OUTPUT = "checks/case-6-epoch-248-epoch158-method/epoch_248_epoch158_method.csv"
DEFAULT_SUMMARY_OUTPUT = "checks/case-6-epoch-248-epoch158-method/summary.json"
DEFAULT_INFERENCE_SLOT_INDEX = 1
DEFAULT_CONFIRMATION_REWARD_FROM_EPOCH = 248

DECAY_EXPONENTS = {
    Decimal("-0.000475"): Decimal("0.9995251127946402"),
    Decimal("-0.000001"): Decimal("0.9999990000005"),
    Decimal("0.0001"): Decimal("1.0001000050001667"),
    Decimal("0"): Decimal("1"),
}
P0_MULTIPLIERS = {
    50: (1, 20),
    100: (1, 10),
    200: (1, 5),
    300: (3, 10),
    400: (2, 5),
    500: (1, 2),
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
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/json", **(headers or {})},
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
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
    at_height: int | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    url = endpoint(node_url, path)
    headers = {"x-cosmos-block-height": str(at_height)} if at_height else None
    while True:
        payload = fetch_json(url, timeout=timeout, retries=retries, headers=headers)
        items.extend(payload.get(item_key, []) or [])
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


def to_gnk(base_units: int | Decimal) -> str:
    return str(Decimal(base_units) / Decimal(1_000_000_000))


def parse_chain_decimal(value: Any) -> Decimal:
    if isinstance(value, dict) and "value" in value and "exponent" in value:
        return Decimal(str(value["value"])) * (Decimal(10) ** int(value["exponent"]))
    if value is None:
        return Decimal(0)
    return Decimal(str(value))


def calculate_fixed_epoch_reward(epoch: int, params: dict[str, Any]) -> int:
    bitcoin_params = params["bitcoin_reward_params"]
    initial_reward = Decimal(str(bitcoin_params["initial_epoch_reward"]))
    genesis_epoch = int(bitcoin_params["genesis_epoch"])
    decay_rate = parse_chain_decimal(bitcoin_params["decay_rate"])
    epochs_since_genesis = max(epoch - genesis_epoch, 0)
    if epochs_since_genesis == 0:
        return int(initial_reward)
    exponent = DECAY_EXPONENTS.get(decay_rate)
    if exponent is None:
        raise RuntimeError(f"unsupported decay rate: {decay_rate}")
    return int((initial_reward * (exponent ** epochs_since_genesis)).to_integral_value(rounding=ROUND_DOWN))


def slot_stats(
    groups: list[dict[str, Any]],
    epoch: int,
    slot_index: int,
) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for group in groups:
        if to_int(group.get("epoch_index")) != epoch or group.get("model_id", "") == "":
            continue
        for vw in group.get("validation_weights", []) or []:
            address = vw["member_address"]
            row = result.setdefault(
                address,
                {
                    "inference_slot_nodes": 0,
                    "inference_slot_weight": 0,
                    "verification_slot_nodes": 0,
                    "verification_slot_weight": 0,
                    "all_mlnode_weight": 0,
                },
            )
            for node in vw.get("ml_nodes", []) or []:
                slots = node.get("timeslot_allocation") or []
                weight = to_int(node.get("poc_weight"))
                row["all_mlnode_weight"] += weight
                if len(slots) > 0 and bool(slots[0]):
                    row["verification_slot_nodes"] += 1
                    row["verification_slot_weight"] += weight
                if len(slots) > slot_index and bool(slots[slot_index]):
                    row["inference_slot_nodes"] += 1
                    row["inference_slot_weight"] += weight
    return result


def performance_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["participant_id"]: row for row in rows if row.get("participant_id")}


def decimal_floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


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
    return max(governance_permille, selected), selected == 500


def missed_stat_test_passed(missed: int, total: int, p0_permille: int) -> bool:
    if total == 0:
        return True
    if missed < 0 or total < 0 or missed > total:
        return False
    if total > 990:
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
    if len(positive) == 2:
        max_percentage = Decimal("0.50")
    elif len(positive) == 3:
        max_percentage = Decimal("0.40")
    sorted_weights = sorted(weight for _, weight in positive)
    cap: int | None = None
    sum_prev = 0
    for index, current_weight in enumerate(sorted_weights):
        weighted_total = sum_prev + current_weight * (len(positive) - index)
        threshold = max_percentage * Decimal(weighted_total)
        if Decimal(current_weight) > threshold:
            numerator = max_percentage * Decimal(sum_prev)
            denominator = Decimal(1) - max_percentage * Decimal(len(positive) - index)
            cap = current_weight if denominator <= 0 else int((numerator / denominator).to_integral_value(rounding=ROUND_DOWN))
            break
        sum_prev += current_weight
    if cap is None:
        return weights.copy()
    result = weights.copy()
    for address, weight in positive:
        if weight > cap:
            result[address] = cap
    return result


def calculate(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    node_url = args.node_url
    epoch = args.epoch
    params = fetch_json(
        endpoint(node_url, "/chain-api/productscience/inference/inference/params"),
        timeout=args.timeout,
        retries=args.retries,
    )["params"]
    current_groups = fetch_paginated(
        node_url,
        "/chain-api/productscience/inference/inference/epoch_group_data",
        "epoch_group_data",
        timeout=args.timeout,
        retries=args.retries,
    )
    epoch_groups = [g for g in current_groups if to_int(g.get("epoch_index")) == epoch]
    root_group = next((g for g in epoch_groups if g.get("model_id", "") == ""), None)
    if root_group is None:
        raise RuntimeError(f"missing root epoch group for epoch {epoch}")
    effective_height = to_int(root_group.get("effective_block_height"))

    try:
        historical_groups = fetch_paginated(
            node_url,
            "/chain-api/productscience/inference/inference/epoch_group_data",
            "epoch_group_data",
            timeout=args.timeout,
            retries=args.retries,
            at_height=effective_height,
        )
        historical_available = True
        snapshot_error = ""
    except RuntimeError as exc:
        historical_groups = []
        historical_available = False
        snapshot_error = str(exc)

    performance = fetch_json(
        endpoint(
            node_url,
            f"/chain-api/productscience/inference/inference/epoch_performance_summary/{epoch}",
        ),
        timeout=args.timeout,
        retries=args.retries,
    )["epochPerformanceSummary"]
    performance_by_address = performance_map(performance)
    exclusion_payload = fetch_json(
        endpoint(
            node_url,
            f"/chain-api/productscience/inference/inference/excluded_participants/{epoch}",
        ),
        timeout=args.timeout,
        retries=args.retries,
    )
    excluded_by_address = {
        item["address"]: item.get("reason", "excluded")
        for item in exclusion_payload.get("items", [])
        if item.get("address")
    }

    fixed_epoch_reward = calculate_fixed_epoch_reward(epoch, params)
    total_full_weight = to_int(root_group.get("total_weight"))
    coefficients = model_coefficients(params)
    governance_p0 = parse_chain_decimal(
        params.get("validation_params", {}).get("binom_test_p0", {"value": "1", "exponent": -1})
    )
    p0_permille, skip_punishment = dynamic_p0_permille(
        performance,
        ceil_supported_p0_permille(int((governance_p0 * Decimal(1000)).to_integral_value(rounding=ROUND_FLOOR))),
    )
    current_slot = slot_stats(epoch_groups, epoch, args.inference_slot_index)
    historical_slot = slot_stats(historical_groups, epoch, args.inference_slot_index)
    root_by_address = {
        vw["member_address"]: vw for vw in root_group.get("validation_weights", [])
    }
    raw_totals: dict[str, int] = {}
    for group in epoch_groups:
        model_id = group.get("model_id", "")
        if model_id == "":
            continue
        for vw in group.get("validation_weights", []) or []:
            address = vw["member_address"]
            raw_totals[address] = raw_totals.get(address, 0) + coefficient_adjusted_weight(
                vw,
                model_id,
                coefficients,
            )

    participant_addresses = sorted(root_by_address)
    effective_by_address: dict[str, int] = {}
    for address in participant_addresses:
        vw = root_by_address[address]
        weight = max(0, to_int(vw.get("weight")))
        confirmation_weight = max(0, to_int(vw.get("confirmation_weight")))
        current_inference_weight = current_slot.get(address, {}).get("inference_slot_weight", 0)
        if address in excluded_by_address or weight <= 0:
            effective_by_address[address] = 0
        elif epoch < args.confirmation_reward_from_epoch:
            effective_by_address[address] = min(
                weight,
                confirmation_weight + current_inference_weight,
            )
        else:
            raw_total = raw_totals.get(address, 0)
            if raw_total > 0 and weight < raw_total:
                effective_weight = confirmation_weight * weight // raw_total
            else:
                effective_weight = confirmation_weight
            effective_by_address[address] = min(
                weight,
                max(0, effective_weight),
            )

    capped = apply_power_capping(effective_by_address)
    filtered: dict[str, int] = {}
    for address in participant_addresses:
        perf = performance_by_address.get(address, {})
        total = to_int(perf.get("inference_count")) + to_int(perf.get("missed_requests"))
        missed = to_int(perf.get("missed_requests"))
        filtered[address] = (
            capped.get(address, 0)
            if skip_punishment or missed_stat_test_passed(missed, total, p0_permille)
            else 0
        )
    simulated_reward = {
        address: (
            filtered[address] * fixed_epoch_reward // total_full_weight
            if total_full_weight > 0 and filtered[address] > 0
            else 0
        )
        for address in participant_addresses
    }
    total_simulated_reward = sum(simulated_reward.values())
    total_non_inference_weight = sum(
        max(0, to_int(root_by_address[address].get("confirmation_weight")))
        for address in participant_addresses
    )
    global_coeff = (
        Decimal(total_simulated_reward) / Decimal(total_non_inference_weight)
        if total_non_inference_weight > 0
        else Decimal(0)
    )

    rows: list[dict[str, Any]] = []
    for address in participant_addresses:
        vw = root_by_address[address]
        current = current_slot.get(address, {})
        historical = historical_slot.get(address, {})
        current_inf_weight = int(current.get("inference_slot_weight", 0))
        historical_inf_weight = int(historical.get("inference_slot_weight", 0))
        lost_inf_weight = max(historical_inf_weight - current_inf_weight, 0)
        estimated_lost = (
            Decimal(lost_inf_weight) * global_coeff
            if epoch < args.confirmation_reward_from_epoch
            else Decimal(0)
        )
        expected_lost_ngonka = int(estimated_lost.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        reward = to_int(performance_by_address.get(address, {}).get("rewarded_coins"))
        rows.append(
            {
                "participant_index": address,
                "epoch": epoch,
                "snapshot_available": historical_available,
                "snapshot_error": snapshot_error,
                "effective_block_height": effective_height,
                "real_reward_chain": reward,
                "simulated_reward_chain_formula": simulated_reward[address],
                "parent_base_weight": to_int(vw.get("weight")),
                "parent_confirmation_weight": to_int(vw.get("confirmation_weight")),
                "current_inference_slot_weight": current_inf_weight,
                "historical_inference_slot_weight": historical_inf_weight,
                "lost_inference_slot_weight": lost_inf_weight,
                "expected_lost_reward_ngnk": expected_lost_ngonka,
                "expected_lost_reward_gnk": to_gnk(expected_lost_ngonka),
                "excluded_reason": excluded_by_address.get(address, ""),
                "inference_count": to_int(performance_by_address.get(address, {}).get("inference_count")),
                "missed_requests": to_int(performance_by_address.get(address, {}).get("missed_requests")),
            }
        )

    summary = {
        "epoch": epoch,
        "effective_block_height": effective_height,
        "historical_snapshot_available": historical_available,
        "historical_snapshot_error": snapshot_error,
        "participants_total": len(rows),
        "participants_with_lost_inference_slot_weight": sum(
            1 for row in rows if int(row["lost_inference_slot_weight"]) > 0
        ),
        "total_lost_inference_slot_weight": sum(
            int(row["lost_inference_slot_weight"]) for row in rows
        ),
        "participants_with_positive_expected_lost_reward": sum(
            1 for row in rows if int(row["expected_lost_reward_ngnk"]) > 0
        ),
        "total_expected_lost_reward_ngnk": sum(
            int(row["expected_lost_reward_ngnk"]) for row in rows
        ),
        "total_expected_lost_reward_gnk": to_gnk(
            sum(int(row["expected_lost_reward_ngnk"]) for row in rows)
        ),
        "participants_reward_mismatch_count": sum(
            1 for row in rows if int(row["real_reward_chain"]) != int(row["simulated_reward_chain_formula"])
        ),
        "global_reward_per_weight_coefficient": str(global_coeff),
    }
    return rows, summary


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("participant_index\n", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-url", default=DEFAULT_NODE_URL)
    parser.add_argument("--epoch", type=int, default=DEFAULT_EPOCH)
    parser.add_argument("--inference-slot-index", type=int, default=DEFAULT_INFERENCE_SLOT_INDEX)
    parser.add_argument(
        "--confirmation-reward-from-epoch",
        type=int,
        default=DEFAULT_CONFIRMATION_REWARD_FROM_EPOCH,
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--summary-output", default=DEFAULT_SUMMARY_OUTPUT)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--retries", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows, summary = calculate(args)
    write_csv(Path(args.output), rows)
    Path(args.summary_output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.summary_output).write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.output}")
    print(f"wrote {args.summary_output}")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
