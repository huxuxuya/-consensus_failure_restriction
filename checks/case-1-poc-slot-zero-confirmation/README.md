# Case 1: POC_SLOT Hosts With Zero Confirmation Weight

## Bug

During the epoch `247` migration, hosts that had `POC_SLOT = true` could have
`confirmation_weight = 0`. That lowered or zeroed their reward for epoch `247`.

## Fix Model

This check treats affected hosts as participants that:

- are present in `epoch_group_data/247`;
- have at least one MLNode with `timeslot_allocation[1] = true`;
- have `confirmation_weight = 0`;
- have positive `weight`.

For each affected host, the check estimates the reward as if confirmation weight
had been restored to the host full weight, then applies the epoch downtime
punishment check:

```text
expected_effective_weight = weight
raw_expected_reward = floor(expected_effective_weight * fixed_epoch_reward / total_epoch_weight)
expected_reward = raw_expected_reward if downtime check passed, otherwise 0
compensation = max(0, expected_reward - actual_rewarded_coins)
```

## Run

```bash
python3 calculate_case_1.py
```

Default output:

```text
case_1_compensation.csv
```

## Current Result

Latest run:

```text
candidate rows = 34
rows with positive compensation = 0
total compensation = 0 GNK
```

The previous positive candidate,
`gonka12gc47yq8m7rnsa3aucq8mlzm7men8jaac7qkkz`, would still receive zero reward
after restoration because it fails the epoch `247` downtime check:

```text
total requests = 170
missed requests = 26
downtime check passed = false
```

## Notes

This is a migration-specific check. It does not use the `INVALID` participant
filter from the main consecutive-failure compensation case.
