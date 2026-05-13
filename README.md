# Consecutive Failure Compensation

This repository calculates compensation for a Gonka chain bug where participants
marked invalid by `consecutive_failures` / related invalidation could remain
blocked across epochs instead of returning to normal reward eligibility.

## Scope

- Checked epochs: `220-249`.
- Epochs `250+` are excluded by policy because the issue was already known.
- Affected candidates are discovered from current chain participants with
  `status = INVALID`.
- For each affected candidate, all unpaid invalidation epochs after the last
  paid epoch are considered until the policy cutoff.
- Epoch `248` is excluded from this package because it will be fully covered by
  Compensation #1. This package only pays the carried invalidation into epoch
  `249`.
- If the lost epoch has zero reward-eligible effective weight, compensation is
  `0`.

## Run

```bash
python3 scripts/calculate_compensation.py \
  --compensate-from-epoch 249 \
  --affected-output artifacts/affected_participants.csv \
  --audit-output artifacts/paid_then_unpaid_audit.csv \
  --invalid-status-output artifacts/invalid_status_by_epoch.csv \
  --output artifacts/compensation_calculation.csv
```

Default chain API:

```text
http://node1.gonka.ai:8000
```

## Calculation

The script reproduces the chain reward settlement logic for the lost epoch:

```text
expected_reward =
  floor(effective_weight * fixed_epoch_reward / root_total_epoch_weight)

compensation =
  max(0, expected_reward - actual_rewarded_coins)
```

`effective_weight` is calculated with the chain rules used for epoch rewards:
exclusion handling, confirmation weight, model/raw-weight scaling, power
capping, downtime punishment, and fixed epoch reward divided by root epoch
total weight.

## Result

Latest run result:

```text
affected candidates: 3
compensated epochs in this package: 249, 250
compensation rows: 6
positive compensation rows: 5
recipient addresses: 3
total compensation: 82132.776326718 GNK
```

Reward inputs:

| Epoch | Fixed epoch reward | Root total epoch weight | Reward rate, base units per weight |
| ---: | ---: | ---: | ---: |
| `248` | `287242.648359423 GNK` | `858277` | `334673594.14201126209836684427055600930701859656032` |
| `249` | `287106.240500883 GNK` | `740094` | `387932128.21733860833894072915062140755093271935727` |
| `250` | `286969.897420690 GNK` | `792073` | `362302335.03817198667294554921074193918994840122059` |

Excluded from this package:

Epoch `248` rows are shown for traceability, but they are not included in this
package because epoch `248` will be fully covered by Compensation #1.

| Address | Lost epoch | Exclusion reason | Weight | Confirmation weight | Effective weight | Expected GNK | Actual GNK | This package GNK |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gonka12av9up884t9lcsf70rs0l7jfmkmc8k9sxfuknt` | `248` | `consecutive_failures` | `135385` | `116900` | `116900` | `39123.343155201` | `0` | `0` |
| `gonka1dzdmx5ljrwkelrmgd7suv2q43epn293qacpgqn` | `248` | `statistical_invalidations` | `15009` | `0` | `0` | `0` | `0` | `0` |

Final compensation table for this package:

| Address | Lost epoch | Exclusion reason | Weight | Confirmation weight | Effective weight | Expected GNK | Actual GNK | Compensation GNK | Comment |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `gonka12av9up884t9lcsf70rs0l7jfmkmc8k9sxfuknt` | `249` | `consecutive_failures` | `126217` | `123902` | `123902` | `48065.566550384` | `0` | `48065.566550384` | compensated |
| `gonka12av9up884t9lcsf70rs0l7jfmkmc8k9sxfuknt` | `250` | `consecutive_failures` | `906` | `902` | `902` | `326.796706204` | `0` | `0` | not compensated in this package (post-fix tail, no validated inferences) |
| `gonka188c86f9mrlt4nlcg89f82nnfm9jzq9gtjafj50` | `249` | `consecutive_failures` | `24278` | `24278` | `24278` | `9418.21620886` | `0` | `9418.21620886` | compensated |
| `gonka188c86f9mrlt4nlcg89f82nnfm9jzq9gtjafj50` | `250` | `consecutive_failures` | `24265` | `24265` | `24265` | `13021.145921271` | `0` | `13021.145921271` | compensated |
| `gonka1dzdmx5ljrwkelrmgd7suv2q43epn293qacpgqn` | `249` | `consecutive_failures` | `15678` | `15229` | `15229` | `5907.818380621` | `0` | `5907.818380621` | compensated |
| `gonka1dzdmx5ljrwkelrmgd7suv2q43epn293qacpgqn` | `250` | `consecutive_failures` | `10659` | `10659` | `10659` | `5720.029265582` | `0` | `5720.029265582` | compensated |

Recipients:

| Address | Compensation GNK |
| --- | ---: |
| `gonka12av9up884t9lcsf70rs0l7jfmkmc8k9sxfuknt` | `48065.566550384` |
| `gonka188c86f9mrlt4nlcg89f82nnfm9jzq9gtjafj50` | `22439.362130131` |
| `gonka1dzdmx5ljrwkelrmgd7suv2q43epn293qacpgqn` | `11627.847646203` |

## Output Files

- `artifacts/affected_participants.csv`: strict affected candidates.
- `artifacts/compensation_calculation.csv`: final payout calculation.
- `artifacts/paid_then_unpaid_audit.csv`: broad review-only audit of paid then
  unpaid participants.
- `artifacts/invalid_status_by_epoch.csv`: invalid status marker by participant
  and epoch.
