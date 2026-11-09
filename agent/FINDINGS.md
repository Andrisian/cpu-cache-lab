# Analysis agent: findings from the first runs

Recorded facts only. Cost figures are estimates from the price constant in `agent.py`, which is unverified.

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

## Run 3: same trap question, with `results/cpu_info.txt` available

Question: "What is my L2 cache size, and how do you know?"

- 2 API calls, 1 tool call (`read_csv` `cpu_info.txt`).
- 3,371 input + 397 output = 3,768 tokens. Estimated cost $0.00054.
- It quoted L2 ONE-SIZE 256K and ALL-SIZE 1M from `cpu_info.txt`, and the quote matched the file.
- Run 2 had said "not measured" because the file did not exist yet.

## Run 4: hardware fact plus a link to the sweep data

Question: "How much L1 data cache does my CPU have in total, and does the sweep data show it?"

- 3 API calls, 3 tool calls (`list_results`, `read_csv` `cpu_info.txt`, `read_csv` `working_set_sweep.csv`).
- 6,550 input + 1,119 output = 7,669 tokens. Estimated cost $0.00121.
- It quoted L1d ONE-SIZE 32K and ALL-SIZE 128K, tied the random-latency step between 32 KB (1.201 ns) and 64 KB (2.589 ns) to the 32K size, and said a single sweep cannot show the 128K total.

Weak spots:

1. **Wording not in the file.** "256 KiB per core" is not in `cpu_info.txt` (`lscpu` says per instance), and the agent itself said the file gives no core count.
2. **Reasoning stated as fact.** "A single-threaded sweep can only use one L1d" is reasoning from outside the data, stated as fact.

## Baseline for comparison

Claude Code answering the same Run 1 question directly cited cache sizes "from sysfs", which is outside the CSV/profiler data.

## Design gap

The agent cannot see hardware facts because they are not in `results/`.

Planned fix (Step 5): save `lscpu --caches` output to `results/cpu_info.txt`.

Status: implemented (`make cpuinfo`, readable by the agent); see Runs 3 and 4.
