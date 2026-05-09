# Case 6: Epoch 248 Check Using Epoch158 Method

## Purpose

Run the same style of check as `huxuxuya/epoch158`, but only for epoch `248`.

The method:

1. Reads current epoch `248` data.
2. Reads historical epoch `248` data at `effective_block_height` using
   `x-cosmos-block-height`.
3. Compares current and historical `POC_SLOT` / inference-slot weight.
4. Calculates lost inference-slot weight.
5. Estimates lost reward using a global reward-per-weight coefficient, matching
   the epoch158 repository approach.

## Run

```bash
python3 calculate_epoch_248_epoch158_method.py --node-url http://gonka.spv.re:8000
```

Outputs:

```text
epoch_248_epoch158_method.csv
summary.json
```

## Important Columns

```text
participant_index
snapshot_available
snapshot_error
effective_block_height
real_reward_chain
simulated_reward_chain_formula
parent_base_weight
parent_confirmation_weight
current_inference_slot_weight
historical_inference_slot_weight
lost_inference_slot_weight
expected_lost_reward_ngnk
expected_lost_reward_gnk
excluded_reason
inference_count
missed_requests
```

This is an audit table, not a payout policy by itself.

## Latest Result

Latest run against archive node `http://gonka.spv.re:8000`:

```text
epoch: 248
effective_block_height: 3828723
historical_snapshot_available: true
participants_total: 95
participants_with_lost_inference_slot_weight: 0
total_expected_lost_reward_gnk: 0
participants_reward_mismatch_count: 0
```

This check is relevant because the `v0.2.12` software upgrade was executed
inside epoch `248` at block `3834200`.

Conclusion: for epoch `248`, the epoch158-style historical snapshot check does
not find any node whose historical inference-slot weight was higher than its
current inference-slot weight. Since epoch `248` already uses the
confirmation-weight reward path, inference-slot weight is kept only as an audit
field and does not create additional compensation in this case.
