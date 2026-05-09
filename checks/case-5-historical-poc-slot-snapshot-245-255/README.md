# Case 5: Historical POC_SLOT Snapshot Audit 245-255

## Purpose

Check whether current epoch group data lost historical `POC_SLOT` /
inference-slot preserved weight.

This is modeled after the epoch `158` repository approach: for each checked
epoch, the script reads current epoch group data and also reads epoch group data
at the epoch `effective_block_height` using the `x-cosmos-block-height` header.

## Run

```bash
python3 calculate_historical_poc_slot_snapshot.py
```

Default outputs:

```text
historical_poc_slot_snapshot_245_255.csv
historical_poc_slot_snapshot_wide_245_255.csv
```

## Columns

Important long-table columns:

```text
epoch
address
effective_block_height
weight
confirmation_weight
current_poc_slot_weight
historical_poc_slot_weight
lost_poc_slot_weight
current_effective_weight
restored_effective_weight
restored_effective_weight_after_filters
filter_reason
actual_reward_gnk
current_expected_reward_gnk
restored_expected_reward_gnk
historical_delta_reward_gnk
```

`historical_delta_reward_gnk` is the possible reward delta after restoring
historical POC_SLOT weight, while still applying exclusion and downtime filters.

This is an audit table, not a payout policy by itself.

## Latest Result

The check was implemented, but the public node used for the run did not expose
the required historical state for epochs `245-255`.

Latest run:

```text
rows: 11
positive_delta_rows: 0
total_historical_delta_reward_gnk: 0
```

All rows are marked:

```text
snapshot_status = unavailable
filter_reason = historical_snapshot_unavailable
```

The node returned pruned-state errors for the required `effective_block_height`
snapshots. Because of that, this run cannot prove whether additional
compensation exists or not. It only proves that the public node used for the run
does not retain the historical snapshots needed for this check.

To complete this audit, rerun the script against an archive node that can serve
`x-cosmos-block-height` queries for the checked epoch effective heights.
