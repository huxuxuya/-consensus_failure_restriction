#!/usr/bin/env python3
"""Audit lost POC_SLOT weight using historical epoch-group snapshots."""

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
DEFAULT_OUTPUT = (
    "checks/case-5-historical-poc-slot-snapshot-245-255/"
    "historical_poc_slot_snapshot_245_255.csv"
)
DEFAULT_WIDE_OUTPUT = (
    "checks/case-5-historical-poc-slot-snapshot-245-255/"
    "historical_poc_slot_snapshot_wide_245_255.csv"
)
DEFAULT_INFERENCE_SLOT_INDEX = 1
DEFAULT_CONFIRMATION_REWARD_FROM_EPOCH = 248
LONG_FIELDNAMES = [
    "epoch",
    "address",
    "snapshot_status",
    "snapshot_error",
    "effective_block_height",
    "weight",
    "confirmation_weight",
    "current_poc_slot_weight",
    "historical_poc_slot_weight",
    "lost_poc_slot_weight",
    "current_effective_weight",
    "restored_effective_weight",
    "restored_effective_weight_after_filters",
    "filter_reason",
    "actual_reward_gnk",
    "current_expected_reward_gnk",
    "restored_expected_reward_gnk",
    "historical_delta_reward_gnk",
]
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
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/json", **(headers or {})},
            )
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
    at_height: int | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    url = endpoint(node_url, path)
    headers = {"x-cosmos-block-height": str(at_height)} if at_height else None
    while True:
        payload = fetch_json(url, timeout=timeout, retries=retries, headers=headers)
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


def slot_weight(validation_weight: dict[str, Any], slot_index: int) -> int:
    total = 0
    for node in validation_weight.get("ml_nodes", []):
        slots = node.get("timeslot_allocation") or []
        if len(slots) > slot_index and bool(slots[slot_index]):
            total += to_int(node.get("poc_weight"))
    return total


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


def downtime_passed(
    performance: dict[str, Any],
    p0_permille: int,
    skip_punishment: bool,
) -> bool:
    if skip_punishment:
        return True
    total = to_int(performance.get("inference_count")) + to_int(performance.get("missed_requests"))
    missed = to_int(performance.get("missed_requests"))
    return missed_stat_test_passed(missed, total, p0_permille)


def load_epoch_groups_by_epoch(
    args: argparse.Namespace,
    at_height: int | None = None,
) -> dict[int, list[dict[str, Any]]]:
    groups = fetch_paginated(
        args.node_url,
        "/chain-api/productscience/inference/inference/epoch_group_data",
        "epoch_group_data",
        timeout=args.timeout,
        retries=args.retries,
        at_height=at_height,
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


def slot_weights_by_address(
    groups: list[dict[str, Any]],
    slot_index: int,
) -> dict[str, int]:
    result: dict[str, int] = {}
    for group in groups:
        if group.get("model_id", "") == "":
            continue
        for vw in group.get("validation_weights", []):
            address = vw["member_address"]
            result[address] = result.get(address, 0) + slot_weight(vw, slot_index)
    return result


def calculate(args: argparse.Namespace) -> list[dict[str, Any]]:
    params = fetch_json(
        endpoint(args.node_url, "/chain-api/productscience/inference/inference/params"),
        timeout=args.timeout,
        retries=args.retries,
    )["params"]
    governance_p0 = parse_chain_decimal(
        params.get("validation_params", {}).get("binom_test_p0", {"value": "1", "exponent": -1})
    )
    governance_p0_permille = ceil_supported_p0_permille(
        int((governance_p0 * Decimal(1000)).to_integral_value(rounding=ROUND_FLOOR))
    )
    current_by_epoch = load_epoch_groups_by_epoch(args)
    historical_cache: dict[int, dict[int, list[dict[str, Any]]]] = {}
    rows: list[dict[str, Any]] = []

    for epoch in range(args.from_epoch, args.to_epoch + 1):
        current_groups = current_by_epoch.get(epoch, [])
        root_group = next((g for g in current_groups if g.get("model_id", "") == ""), None)
        if root_group is None:
            continue
        effective_height = to_int(root_group.get("effective_block_height"))
        if effective_height > 0:
            historical_by_epoch = historical_cache.get(effective_height)
            if historical_by_epoch is None:
                try:
                    historical_by_epoch = load_epoch_groups_by_epoch(
                        args,
                        at_height=effective_height,
                    )
                except RuntimeError as exc:
                    rows.append(
                        {
                            "epoch": epoch,
                            "address": "__epoch_snapshot_unavailable__",
                            "snapshot_status": "unavailable",
                            "snapshot_error": str(exc),
                            "effective_block_height": effective_height,
                            "weight": 0,
                            "confirmation_weight": 0,
                            "current_poc_slot_weight": 0,
                            "historical_poc_slot_weight": 0,
                            "lost_poc_slot_weight": 0,
                            "current_effective_weight": 0,
                            "restored_effective_weight": 0,
                            "restored_effective_weight_after_filters": 0,
                            "filter_reason": "historical_snapshot_unavailable",
                            "actual_reward_gnk": "0",
                            "current_expected_reward_gnk": "0",
                            "restored_expected_reward_gnk": "0",
                            "historical_delta_reward_gnk": "0",
                        }
                    )
                    continue
                historical_cache[effective_height] = historical_by_epoch
            historical_groups = historical_by_epoch.get(epoch, [])
        else:
            historical_groups = []

        current_slot = slot_weights_by_address(
            current_groups,
            args.inference_slot_index,
        )
        historical_slot = slot_weights_by_address(
            historical_groups,
            args.inference_slot_index,
        )
        performance = fetch_json(
            endpoint(
                args.node_url,
                f"/chain-api/productscience/inference/inference/epoch_performance_summary/{epoch}",
            ),
            timeout=args.timeout,
            retries=args.retries,
        )["epochPerformanceSummary"]
        performance_by_address = performance_map(performance)
        p0_permille, skip_punishment = dynamic_p0_permille(performance, governance_p0_permille)
        exclusion_reasons = load_exclusion_reasons(args, epoch)

        fixed_epoch_reward = calculate_fixed_epoch_reward(epoch, params)
        total_epoch_weight = to_int(root_group.get("total_weight"))
        root_weights = {
            vw["member_address"]: vw for vw in root_group.get("validation_weights", [])
        }
        addresses = sorted(set(root_weights) | set(current_slot) | set(historical_slot))

        for address in addresses:
            vw = root_weights.get(address, {})
            weight = max(0, to_int(vw.get("weight")))
            confirmation_weight = max(0, to_int(vw.get("confirmation_weight")))
            current_poc_slot_weight = max(0, current_slot.get(address, 0))
            historical_poc_slot_weight = max(0, historical_slot.get(address, 0))
            lost_poc_slot_weight = max(
                0,
                historical_poc_slot_weight - current_poc_slot_weight,
            )
            actual_reward = to_int(
                performance_by_address.get(address, {}).get("rewarded_coins")
            )
            current_effective_weight = min(
                weight,
                confirmation_weight + current_poc_slot_weight,
            )
            restored_effective_weight = min(
                weight,
                confirmation_weight + historical_poc_slot_weight,
            )
            if address in exclusion_reasons:
                restored_effective_weight_after_filters = 0
                filter_reason = f"excluded:{exclusion_reasons[address]}"
            elif not downtime_passed(
                performance_by_address.get(address, {}),
                p0_permille,
                skip_punishment,
            ):
                restored_effective_weight_after_filters = 0
                filter_reason = "downtime_punishment"
            else:
                restored_effective_weight_after_filters = restored_effective_weight
                filter_reason = ""

            current_expected_reward = (
                current_effective_weight * fixed_epoch_reward // total_epoch_weight
                if total_epoch_weight > 0 and current_effective_weight > 0
                else 0
            )
            restored_expected_reward = (
                restored_effective_weight_after_filters
                * fixed_epoch_reward
                // total_epoch_weight
                if total_epoch_weight > 0 and restored_effective_weight_after_filters > 0
                else 0
            )
            historical_delta_reward = max(
                0,
                restored_expected_reward - actual_reward,
            )
            if (
                lost_poc_slot_weight <= 0
                and historical_delta_reward <= 0
                and current_poc_slot_weight <= 0
                and historical_poc_slot_weight <= 0
            ):
                continue

            rows.append(
                {
                    "epoch": epoch,
                    "address": address,
                    "snapshot_status": "available",
                    "snapshot_error": "",
                    "effective_block_height": effective_height,
                    "weight": weight,
                    "confirmation_weight": confirmation_weight,
                    "current_poc_slot_weight": current_poc_slot_weight,
                    "historical_poc_slot_weight": historical_poc_slot_weight,
                    "lost_poc_slot_weight": lost_poc_slot_weight,
                    "current_effective_weight": current_effective_weight,
                    "restored_effective_weight": restored_effective_weight,
                    "restored_effective_weight_after_filters": restored_effective_weight_after_filters,
                    "filter_reason": filter_reason,
                    "actual_reward_gnk": to_gnk(actual_reward),
                    "current_expected_reward_gnk": to_gnk(current_expected_reward),
                    "restored_expected_reward_gnk": to_gnk(restored_expected_reward),
                    "historical_delta_reward_gnk": to_gnk(historical_delta_reward),
                }
            )

    return sorted(
        rows,
        key=lambda row: (
            Decimal(row["historical_delta_reward_gnk"]),
            int(row["epoch"]),
            row["address"],
        ),
        reverse=True,
    )


def wide_rows(
    rows: list[dict[str, Any]],
    from_epoch: int,
    to_epoch: int,
) -> list[dict[str, Any]]:
    by_address: dict[str, dict[int, dict[str, Any]]] = {}
    for row in rows:
        by_address.setdefault(row["address"], {})[int(row["epoch"])] = row
    result: list[dict[str, Any]] = []
    for address, by_epoch in by_address.items():
        wide: dict[str, Any] = {"address": address}
        total = Decimal(0)
        total_lost_weight = 0
        for epoch in range(from_epoch, to_epoch + 1):
            row = by_epoch.get(epoch)
            if row is None:
                wide[f"epoch_{epoch}_lost_poc_slot_weight"] = ""
                wide[f"epoch_{epoch}_historical_delta_reward_gnk"] = ""
                wide[f"epoch_{epoch}_filter_reason"] = ""
                continue
            delta = Decimal(row["historical_delta_reward_gnk"])
            total += delta
            total_lost_weight += int(row["lost_poc_slot_weight"])
            wide[f"epoch_{epoch}_lost_poc_slot_weight"] = row["lost_poc_slot_weight"]
            wide[f"epoch_{epoch}_historical_delta_reward_gnk"] = row[
                "historical_delta_reward_gnk"
            ]
            wide[f"epoch_{epoch}_filter_reason"] = row["filter_reason"]
        wide["total_lost_poc_slot_weight"] = total_lost_weight
        wide["total_historical_delta_reward_gnk"] = str(total)
        result.append(wide)
    return sorted(
        result,
        key=lambda row: (
            Decimal(row["total_historical_delta_reward_gnk"]),
            int(row["total_lost_poc_slot_weight"]),
            row["address"],
        ),
        reverse=True,
    )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text(",".join(LONG_FIELDNAMES) + "\n", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as file:
        fieldnames = LONG_FIELDNAMES if "snapshot_status" in rows[0] else list(rows[0].keys())
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-url", default=DEFAULT_NODE_URL)
    parser.add_argument("--from-epoch", type=int, default=DEFAULT_FROM_EPOCH)
    parser.add_argument("--to-epoch", type=int, default=DEFAULT_TO_EPOCH)
    parser.add_argument("--inference-slot-index", type=int, default=DEFAULT_INFERENCE_SLOT_INDEX)
    parser.add_argument(
        "--confirmation-reward-from-epoch",
        type=int,
        default=DEFAULT_CONFIRMATION_REWARD_FROM_EPOCH,
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--wide-output", default=DEFAULT_WIDE_OUTPUT)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--retries", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = calculate(args)
    wide = wide_rows(rows, args.from_epoch, args.to_epoch)
    write_csv(Path(args.output), rows)
    write_csv(Path(args.wide_output), wide)
    positive = [row for row in rows if Decimal(row["historical_delta_reward_gnk"]) > 0]
    total = sum((Decimal(row["historical_delta_reward_gnk"]) for row in positive), Decimal(0))
    print(f"wrote {args.output}")
    print(f"wrote {args.wide_output}")
    print(f"rows: {len(rows)}")
    print(f"positive_delta_rows: {len(positive)}")
    print(f"total_historical_delta_reward_gnk: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
