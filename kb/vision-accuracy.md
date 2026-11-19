<!-- source: claude -->
# Vision pipeline accuracy (measured 2026-11-19)

Exact-match scores from the saved benchmark runs (`bench_results/h_minicpm`, `h_qwen3vl`, 154 human-labelled photos). The golden labels still contain known errors (see `golden_second_check_20260922.md`), so these are a floor. Re-run with `python3 scripts/vision_eval.py` on Tokyo7 (no GPU, read-only).

## Exact match per field
| Field | MiniCPM-V (production) | Qwen3-VL 8B | When both agree: share of photos / right |
| --- | --- | --- | --- |
| Brand | 82.4% | 66.4% | 66.4% / 95.2% |
| Model | 41.6% | 34.4% | 22.7% / 88.6% |
| Serial | 49.7% | 51.0% | 36.4% / 96.4% |

The old scorer's "fuzzy hit" counted near-misses as right (model 57%, serial 67%); exact match is what parts and warranty lookups need.

## What the numbers say
- **Agreement is the best signal we have.** When both models read the same value it is right 89–96% of the time. Auto-accept agreed fields; send disagreements to human review.
- **Character fixes barely help** (O/0, I/1, S/5, B/8): model +0, serial +2 points. The errors are misreads and wrong labels (location sticker, factory address), not lookalike characters.
- **Refrigerant: use Qwen and snap to the valid list** (R-410A, R-22, R-32, R-454B, R-407C, R-134A): 36% (production MiniCPM) → 86%.
- **Tonnage from the model's capacity code is not reliable** (35–40% right, because the model number itself is often misread). Keep MiniCPM's read value (56%). Qwen's 11.5% tonnage is a parsing problem, not vision.
- Next measurable step: fix the golden labels from the second-check list, then train a vision LoRA on our own plates (human labels only).
