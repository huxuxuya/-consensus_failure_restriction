

# Additional Checks

This directory contains standalone verification cases that should not be mixed
with the main consecutive-failure compensation script.

## Cases

| Case | Directory | Status |
| --- | --- | --- |
| POC_SLOT hosts with zero confirmation weight in epoch 247 | `case-1-poc-slot-zero-confirmation` | Scripted |
| Preserved MLNodes with lowered weight in epoch 247 | `case-2-preserved-node-lowered-weight` | Scripted |
| Broad epoch reward loss audit for epochs 245-255 | `case-4-epoch-loss-audit-245-255` | Scripted |
| Historical POC_SLOT snapshot audit for epochs 245-255 | `case-5-historical-poc-slot-snapshot-245-255` | Scripted |
| Epoch 248 historical POC_SLOT check using epoch158 method | `case-6-epoch-248-epoch158-method` | Scripted |

The original consecutive-failure bug is handled by the root script:

```text
scripts/calculate_compensation.py
```
