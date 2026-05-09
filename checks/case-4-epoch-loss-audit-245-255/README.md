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

## Columns

Important columns:

```text
address
total_lost_vs_full_weight_gnk
total_lost_due_to_confirmation_weight_gnk
epochs_with_zero_paid_positive_expected
epochs_with_large_confirmation_loss
epoch_<N>_weight
epoch_<N>_confirmation_weight
epoch_<N>_effective_weight
epoch_<N>_poc_slot_weight
epoch_<N>_excluded
epoch_<N>_actual_reward_gnk
epoch_<N>_expected_full_weight_reward_gnk
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
