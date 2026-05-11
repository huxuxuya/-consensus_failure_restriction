# Case 4: Epoch Loss Audit 245-255

## Purpose

Generate a broad participant table for epochs `245-255` showing epoch-level
data in columns:

- actual paid reward;
- expected reward by full epoch weight;
- expected reward by the chain settlement algorithm;
- reward lost because confirmation weight was lower than full weight;
- participants with zero payout despite positive expected reward;
- participants with large confirmation-weight loss.

This is an audit table, not a payout policy by itself.

## Run

```bash
python3 calculate_epoch_loss_audit.py
```

Default output:

```text
epoch_loss_audit_wide_245_255.csv
confirmation_plus_poc_slot_minus_effective_reward_245_255.csv
chain_expected_delta_245_255.csv
035_bug_fix_expected_minus_actual_245_255.csv
035_bug_fix_weight_minus_effective_weight_245_255.csv
inference_slot_weight_245_255.csv
preserved_event_weight_245_255.csv
```

The default CSV has one participant per row. Epoch data is stored in repeated
columns such as `epoch_245_weight`, `epoch_245_actual_reward_gnk`,
`epoch_245_lost_vs_full_weight_gnk`, then the same columns for epoch `246`,
`247`, and so on through `255`.

To also write the detailed participant-epoch table:

```bash
python3 calculate_epoch_loss_audit.py --long-output epoch_loss_audit_245_255.csv
```

Additional generated files:

```text
confirmation_plus_poc_slot_minus_effective_reward_245_255.csv
```

One participant per row. Epoch columns contain:

```text
epoch_<N>_confirmation_plus_poc_slot_minus_effective_reward_gnk =
  epoch_<N>_expected_confirmation_plus_poc_slot_reward_gnk
  - epoch_<N>_expected_effective_reward_gnk
```

```text
chain_expected_delta_245_255.csv
```

One participant per row. Epoch columns contain:

```text
epoch_<N>_chain_expected_delta_gnk =
  epoch_<N>_expected_effective_reward_gnk
  - epoch_<N>_actual_reward_gnk
```

```text
035_bug_fix_expected_minus_actual_245_255.csv
```

One participant per row. Epoch columns contain:

```text
epoch_<N>_035_bug_fix_expected_minus_actual_gnk =
  epoch_<N>_expected_035_bug_fix_weight_reward_gnk
  - epoch_<N>_actual_reward_gnk
```

Zero values are written as empty cells.

035_bug_fix_weight_minus_effective_weight_245_255.csv
```

One participant per row. Epoch columns compare the corrected diagnostic weight
against the chain effective settlement weight:

```text
epoch_<N>_035_bug_fix_weight_minus_effective_weight =
  epoch_<N>_weight_with_035_bug_fix
  - epoch_<N>_effective_weight
```

Zero values are written as empty cells.

```text
inference_slot_weight_245_255.csv
```

One participant per row. Epoch columns contain the participant's legacy
POC_SLOT / inference-slot weight:

```text
epoch_<N>_inference_slot_weight
```

Zero values are written as empty cells.

```text
preserved_event_weight_245_255.csv
```

One participant per row. For epochs after the confirmation-reward switch, the
old `timeslot_allocation[1]` marker is no longer the source of truth for
inference-serving weight. This compact table contains only weight columns:

```text
epoch_245_poc_slot_allocation_weight
epoch_246_poc_slot_allocation_weight
epoch_247_poc_slot_allocation_weight
epoch_248_poc_slot_allocation_weight
epoch_<N>_event_<K>_preserved_weight
total_preserved_event_weight
```

For `245-248`, weight is read from legacy `timeslot_allocation[1]`.
For `249+`, the script reads historical `PreservedNodesSnapshot` for every
confirmation PoC event and creates event-level columns:

```text
epoch_<N>_event_<K>_preserved_weight
```

The preserved weight is calculated from the event snapshot's preserved
`node_id` list, the epoch model-group MLNode `poc_weight`, and the model weight
coefficient. Empty cells mean the participant had no preserved nodes in that
event.

## Columns

Important columns:

```text
address
total_lost_vs_full_weight_gnk
total_lost_due_to_confirmation_weight_gnk
epochs_with_zero_paid_positive_expected
epochs_with_large_confirmation_loss
epoch_<N>_weight
epoch_<N>_raw_total
epoch_<N>_stuck_035_weight_delta
epoch_<N>_weight_with_035_bug_fix
epoch_<N>_confirmation_weight
epoch_<N>_effective_weight
epoch_<N>_poc_slot_weight
epoch_<N>_excluded
epoch_<N>_actual_reward_gnk
epoch_<N>_expected_full_weight_reward_gnk
epoch_<N>_expected_035_bug_fix_weight_reward_gnk
epoch_<N>_expected_confirmation_weight_reward_gnk
epoch_<N>_expected_confirmation_plus_poc_slot_reward_gnk
epoch_<N>_expected_effective_reward_gnk
epoch_<N>_zero_reward_reason
epoch_<N>_chain_expected_delta_gnk
epoch_<N>_lost_vs_full_weight_gnk
epoch_<N>_lost_due_to_confirmation_weight_gnk
epoch_<N>_zero_paid_with_positive_expected
epoch_<N>_large_confirmation_loss
```

`expected_effective_reward_gnk` is calculated with the settlement rules from
`bitcoin_rewards.go`: active/excluded eligibility, confirmation effective
weight, coefficient-adjusted MLNode scaling, power capping, downtime punishment,
and fixed epoch reward divided by total full epoch weight.

`weight_with_035_bug_fix` is diagnostic and confirmation-aware. For legacy
`POC_SLOT` epochs before the stuck-weight window, it adds preserved slot weight
to confirmation weight. For post-v0.2.12 stuck-weight epochs, it adds back the
detected stale-preserved-node delta and then applies the same `raw_total`
normalization used by the chain settlement effective-weight calculation:

```text
legacy_weight_with_preserved = min(weight, confirmation_weight + poc_slot_weight)

stuck_035_weight_delta = stored_node_poc_weight - floor(stored_node_poc_weight * model_weight_scale_factor)
full_weight_with_035_bug_fix = weight + stuck_035_weight_delta
raw_total_with_035_bug_fix = raw_total + stuck_035_weight_delta
diagnostic_confirmation_weight = confirmation_weight

if diagnostic_confirmation_weight == 0:
  diagnostic_confirmation_weight = raw_total

if full_weight_with_035_bug_fix < raw_total_with_035_bug_fix:
  weight_with_035_bug_fix =
    floor((diagnostic_confirmation_weight + stuck_035_weight_delta)
      * full_weight_with_035_bug_fix
      / raw_total_with_035_bug_fix)
else:
  weight_with_035_bug_fix = diagnostic_confirmation_weight + stuck_035_weight_delta

weight_with_035_bug_fix = min(full_weight_with_035_bug_fix, max(0, weight_with_035_bug_fix))
```

Detection uses epoch `248` as the baseline and marks post-upgrade node weights
as stuck when the same `(model_id, participant, node_id)` remains within
`0.95x-1.10x` of the baseline in epochs `249+`. This column does not by itself
mean compensation is owed; reward eligibility, exclusions, downtime punishment,
power capping, and already-paid rewards still need to be applied.

`expected_035_bug_fix_weight_reward_gnk` is the same diagnostic view converted
to reward units:

```text
floor(weight_with_035_bug_fix * fixed_epoch_reward / total_epoch_weight)
```

It uses the observed epoch denominator. It is not a final net compensation
amount.

`expected_confirmation_weight_reward_gnk` is the simple confirmation-weight
view:

```text
floor(min(weight, confirmation_weight) * fixed_epoch_reward / total_epoch_weight)
```

`expected_confirmation_plus_poc_slot_reward_gnk` is a diagnostic counterfactual
view that adds the POC_SLOT / preserved weight before calculating reward:

```text
floor(min(weight, confirmation_weight + poc_slot_weight) * fixed_epoch_reward / total_epoch_weight)
```

This column does not replace `expected_effective_reward_gnk`. A participant can
still have `expected_effective_reward_gnk = 0` when chain settlement excluded it
or downtime punishment zeroed it.

`zero_reward_reason` is filled when `expected_effective_reward_gnk = 0` and the
participant had positive epoch weight. Common values:

```text
excluded:<reason>
downtime_punishment(<missed_rate>%)
zero_confirmation_weight
zero_effective_weight
zero_expected_reward
```

`chain_expected_delta_gnk = expected_effective_reward_gnk - actual_reward_gnk`.
It should normally be zero. Non-zero rows are formula/debug mismatches, not
compensation rows by themselves.

## Default Flags

`zero_paid_with_positive_expected = true` when actual reward is zero but
expected reward by full weight is positive.

`large_confirmation_loss = true` when confirmation weight causes at least `50%`
loss versus full-weight reward.

## Latest Result

Generated file:

```text
epoch_loss_audit_wide_245_255.csv
```

Summary:

```text
epochs checked: 245-255
participant rows: 182
participant-epoch rows: 925
zero payout with positive full-weight expected reward: 213
large confirmation-weight loss rows: 226
chain reward mismatches: 4
```

The largest total losses are visible at the top of the CSV. This table is
intentionally broad: it shows every participant whose paid reward was lower
than the full-weight expected reward in at least one checked epoch, then the
rows must be reviewed to decide whether the loss is a real bug or an expected
result of confirmation-weight reduction.

The latest formula check matches actual chain rewards for `921 / 925`
participant-epoch rows. The remaining four small mismatches are all in epochs
`245-247`.
