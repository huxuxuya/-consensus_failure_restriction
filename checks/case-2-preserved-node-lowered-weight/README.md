# Case 2: Preserved MLNodes With Lowered Weight

## Bug

MLNodes sampled as preserved nodes in epoch `247` could lose their preserved
weight contribution. The symptom is a host with mixed preserved and non-preserved
MLNodes where reward was calculated from the already-lowered confirmation weight
instead of including the preserved node contribution.

## Fix Model

This check works at host level but inspects MLNode data. It selects hosts that:

- are present in `epoch_group_data/247`;
- have at least one MLNode with `timeslot_allocation[1] = true`;
- have positive `confirmation_weight`;
- have both preserved and non-preserved MLNodes;
- would receive more reward if preserved MLNode weight were restored.

For each candidate, the check estimates reward as if preserved MLNode weight had
been added back to the host effective weight, then applies the epoch downtime
punishment check:

```text
expected_effective_weight = min(weight, confirmation_weight + preserved_poc_weight)
raw_expected_reward = floor(expected_effective_weight * fixed_epoch_reward / total_epoch_weight)
expected_reward = raw_expected_reward if downtime check passed, otherwise 0
compensation = max(0, expected_reward - actual_rewarded_coins)
```

## Run

```bash
python3 calculate_case_2.py
```

Default output:

```text
case_2_compensation.csv
```

## Current Result

Latest run:

```text
candidate rows = 1
total compensation = 475.26866763 GNK
```

Affected address:

```text
gonka19ghzvgfr065s3fr5awuvs3nhy9fq4n7wrr9kel
```

This participant passes the epoch `247` downtime check:

```text
total requests = 307
missed requests = 0
downtime check passed = true
```
