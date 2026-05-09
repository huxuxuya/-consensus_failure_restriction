# Consecutive Failure Compensation

This repository calculates compensation for a Gonka bug where hosts invalidated
with `consecutive_failure` could remain blocked because the invalidation state
was not reset correctly.

## Policy

- Scan epochs `220-249`.
- Do not compensate epochs `250+` because the issue was already known.
- Discover strict affected candidates as current chain participants with
  `status = INVALID`.
- Compensate only the first unpaid epoch after the last paid epoch.
- Pay nothing when the lost epoch has zero reward-eligible weight.

## Run

```bash
python3 scripts/calculate_compensation.py \
  --affected-output artifacts/affected_participants.csv \
  --audit-output artifacts/paid_then_unpaid_audit.csv \
  --output artifacts/compensation_calculation.csv
```

Default source:

```text
http://node1.gonka.ai:8000
```

## Outputs

`artifacts/affected_participants.csv`

Strict affected candidates used for compensation discovery.

`artifacts/paid_then_unpaid_audit.csv`

Broad audit list of every participant with a paid epoch followed by an unpaid
epoch before the cutoff. This file is for review only; most rows are normal
churn, inactive hosts, or zero-weight cases.

`artifacts/compensation_calculation.csv`

Final compensation calculation with all payout inputs.

## Formula

For each lost epoch:

```text
effective_weight = min(weight, confirmation_weight)
reward_rate = fixed_epoch_reward / total_epoch_weight
expected_reward = floor(effective_weight * reward_rate)
compensation = max(0, expected_reward - actual_rewarded_coins)
```

## Current Result

Checked epochs: `220-249` (`30` epochs).

Current chain participants checked: `6739`.

Current participant statuses:

| Status | Count |
| --- | ---: |
| `ACTIVE` | `4635` |
| `INACTIVE` | `1180` |
| `RAMPING` | `921` |
| `INVALID` | `3` |

Broad paid-then-unpaid audit rows: `370`.

Broad audit by current status:

| Status | Count |
| --- | ---: |
| `INACTIVE` | `322` |
| `ACTIVE` | `45` |
| `INVALID` | `3` |

Strict affected candidates: `3`.

Compensation rows: `3`.

Rows with positive compensation: `1`.

Compensated epoch: `248`.

Epoch `248` reward-rate inputs:

| Metric | Value |
| --- | ---: |
| Fixed epoch reward, GNK | `287242.648359423` |
| Total epoch weight | `858277` |
| Reward rate, base units per weight | `334673594.14201126` |

| Address | Last paid | Lost epoch | Weight | Confirmation weight | Effective weight | Reward rate | Expected GNK | Actual GNK | Compensation GNK |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `gonka12av9up884t9lcsf70rs0l7jfmkmc8k9sxfuknt` | `247` | `248` | `135385` | `116900` | `116900` | `334673594.14201126` | `39123.343155201` | `0` | `39123.343155201` |
| `gonka188c86f9mrlt4nlcg89f82nnfm9jzq9gtjafj50` | `247` | `248` | `3887` | `0` | `0` | `334673594.14201126` | `0` | `0` | `0` |
| `` | `247` | `248` | `15009` | `0` | `0` | `334673594.14201126` | `0` | `0` | `0` |
gonka1dzdmx5ljrwkelrmgd7suv2q43epn293qacpgqn
Total compensation:

```text
39123.343155201 GNK
```

## Notes

The script fetches data from chain API on every run and fails on invalid JSON
instead of silently skipping epochs. Reward distribution follows Gonka
`bitcoin_rewards.go`: rewards are proportional to effective participant weight,
divided by total epoch weight. Undistributed shares go to governance, not to the
remaining participants.
