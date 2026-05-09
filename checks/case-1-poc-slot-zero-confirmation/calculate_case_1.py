#!/usr/bin/env python3
"""Calculate case 1 compensation candidates for epoch 247."""

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
DEFAULT_EPOCH = 247
DEFAULT_OUTPUT = "checks/case-1-poc-slot-zero-confirmation/case_1_compensation.csv"
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


def has_poc_slot_true(validation_weight: dict[str, Any]) -> bool:
    for node in validation_weight.get("ml_nodes", []):
        slots = node.get("timeslot_allocation") or []
        if len(slots) > 1 and bool(slots[1]):
            return True
    return False


def performance_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["participant_id"]: row for row in rows if row.get("participant_id")}


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
    if total > MAX_TABLE_N:
        numerator, denominator = P0_MULTIPLIERS[p0_permille]
        return missed * denominator <= total * numerator
    p0 = p0_permille / 1000
    critical = math.floor(
        total * p0 + 1.6448536269514722 * math.sqrt(total * p0 * (1 - p0))
    )
    return missed <= min(critical, total)


def load_epoch_groups(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    groups = fetch_paginated(
        args.node_url,
        "/chain-api/productscience/inference/inference/epoch_group_data",
        "epoch_group_data",
        timeout=args.timeout,
        retries=args.retries,
    )
    epoch_groups = [group for group in groups if to_int(group.get("epoch_index")) == args.epoch]
    root_group = next((group for group in epoch_groups if group.get("model_id", "") == ""), None)
    model_group = next((group for group in epoch_groups if group.get("model_id", "") != ""), None)
    if root_group is None:
        raise RuntimeError(f"missing root epoch group for epoch {args.epoch}")
    if model_group is None:
        raise RuntimeError(f"missing model epoch group for epoch {args.epoch}")
    return root_group, model_group


def calculate(args: argparse.Namespace) -> list[dict[str, Any]]:
    node_url = args.node_url
    params = fetch_json(
        endpoint(node_url, "/chain-api/productscience/inference/inference/params"),
        timeout=args.timeout,
        retries=args.retries,
    )["params"]
    root_group, model_group = load_epoch_groups(args)
    performance = fetch_json(
        endpoint(
            node_url,
            f"/chain-api/productscience/inference/inference/epoch_performance_summary/{args.epoch}",
        ),
        timeout=args.timeout,
        retries=args.retries,
    )["epochPerformanceSummary"]

    total_epoch_weight = to_int(root_group["total_weight"])
    fixed_epoch_reward = calculate_fixed_epoch_reward(args.epoch, params)
    by_participant = performance_map(performance)
    governance_p0 = parse_chain_decimal(
        params.get("validation_params", {}).get("binom_test_p0", {"value": "1", "exponent": -1})
    )
    p0_permille, skip_punishment = dynamic_p0_permille(
        performance,
        ceil_supported_p0_permille(
            int((governance_p0 * Decimal(1000)).to_integral_value(rounding=ROUND_FLOOR))
        ),
    )
    root_weights = {
        row["member_address"]: row for row in root_group.get("validation_weights", [])
    }
    rows: list[dict[str, Any]] = []

    for model_vw in model_group.get("validation_weights", []):
        if not has_poc_slot_true(model_vw):
            continue
        address = model_vw["member_address"]
        vw = root_weights.get(address)
        if not vw:
            continue
        address = vw["member_address"]
        weight = to_int(vw.get("weight"))
        confirmation_weight = to_int(vw.get("confirmation_weight"))
        if weight <= 0 or confirmation_weight != 0:
            continue

        actual = to_int(by_participant.get(address, {}).get("rewarded_coins"))
        performance_row = by_participant.get(address, {})
        total_requests = to_int(performance_row.get("inference_count")) + to_int(
            performance_row.get("missed_requests")
        )
        missed_requests = to_int(performance_row.get("missed_requests"))
        downtime_passed = skip_punishment or missed_stat_test_passed(
            missed_requests,
            total_requests,
            p0_permille,
        )
        expected = (
            decimal_floor(
                Decimal(weight) * Decimal(fixed_epoch_reward) / Decimal(total_epoch_weight)
            )
            if downtime_passed
            else 0
        )
        compensation = max(0, expected - actual)
        rows.append(
            {
                "address": address,
                "epoch": args.epoch,
                "weight": weight,
                "confirmation_weight": confirmation_weight,
                "expected_effective_weight": weight,
                "fixed_epoch_reward": fixed_epoch_reward,
                "total_epoch_weight": total_epoch_weight,
                "total_requests": total_requests,
                "missed_requests": missed_requests,
                "downtime_passed": downtime_passed,
                "actual_rewarded_coins": actual,
                "expected_reward_base_units": expected,
                "compensation_base_units": compensation,
                "compensation_gnk": str(Decimal(compensation) / Decimal(1_000_000_000)),
            }
        )

    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        if rows:
            writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        else:
            file.write("address,epoch,compensation_gnk\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-url", default=DEFAULT_NODE_URL)
    parser.add_argument("--epoch", type=int, default=DEFAULT_EPOCH)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--retries", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = calculate(args)
    write_csv(Path(args.output), rows)
    total = sum(to_int(row["compensation_base_units"]) for row in rows)
    print(f"wrote {args.output}")
    print(f"rows: {len(rows)}")
    print(f"total_compensation_gnk: {Decimal(total) / Decimal(1_000_000_000)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
