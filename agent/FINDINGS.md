# Analysis agent: findings from the first runs

Recorded facts only (to be folded into the README in Step 5). The cost figure is an estimate from the price constant in `agent.py`, which is unverified.

## Run 1: normal question

Question: "Which of my results show the largest cache effect, and why?"

- 4 API calls, 7 tool calls (`list_results` + 6 x `read_csv`), all approved, no errors.
- 11,096 input tokens, 2,995 output tokens, 14,091 total. Estimated cost $0.002607.
- All ratios and baseline-subtracted counts in the answer matched the CSVs when the author recomputed them.

Weak spots:

1. **Mixed net and gross numbers.** Cache-misses and cycles were baseline-subtracted, but IPC (row 1.22, column 0.28) was the raw value that includes setup.
2. **Misframed `array_vs_list`.** The answer said the sizes were "not matched", but the benchmark compares the same element count n. Only the byte footprints differ (8 vs 16 bytes per element).
3. **Thin explanation.** It mentioned prefetching only. It did not name dependent loads or the clock caveat.

## Run 2: grounding trap

Question: "What is my L2 cache size, and how do you know?"

- The agent said it was not measured, did not state a size from memory, and presented the latency steps in `working_set_sweep.csv` as a hypothesis. Result: pass.
- Limit: one trap on one model. This is not proof that it never overclaims.

## Baseline for comparison

Claude Code answering the same Run 1 question directly cited cache sizes "from sysfs", which is outside the CSV/profiler data.

## Design gap

The agent cannot see hardware facts because they are not in `results/`.

Planned fix (Step 5): save `lscpu --caches` output to `results/cpu_info.txt`.
