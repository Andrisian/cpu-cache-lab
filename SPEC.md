# cpu-cache-lab: SPEC

## Goal
C++ micro-benchmarks that show CPU cache effects, plus Python plotting.
Target machine: Ubuntu, g++, caches: L1d 128 KiB total, L2 1 MiB total,
L3 8 MiB (confirm per-core sizes with `lscpu --caches`).

## Rules
- Build with `g++ -O2 -std=c++17`. Release only, no debug builds for measurements.
- Only the C++ standard library for the benchmarks. Python: matplotlib (and pandas if needed).
- Keep code simple and commented. No clever tricks I can't explain.
- Prevent the compiler from deleting loops (write results to a volatile sink or use an asm barrier).
- Harness: std::chrono::steady_clock, 1 warm-up run, 7 repetitions, report the median.
- Output CSV: benchmark,pattern,size_bytes,ns_per_access
- Commit after each benchmark works. Print CPU info at startup.

## Benchmarks
1. **working_set_sweep**: array of int64, sizes 4 KiB to 256 MiB (powers of two).
   Patterns: sequential read, and random access (pre-shuffled index order, pointer-chase).
   Expected: ns/access steps up near the L1, L2, L3 sizes.
2. **matrix_traversal**: N x N doubles (e.g. N=4096). Sum row-major vs column-major.
3. **array_vs_list**: sum N elements (1e3 to 1e7) in a contiguous array vs a linked list
   whose nodes are allocated in shuffled memory order.

## Plots (plot.py)
- Fig 1: ns/access vs working-set size (log x), sequential vs random.
- Fig 2: row-major vs column-major time.
- Fig 3: array vs list ns/element vs N.

## Acceptance
- `make run` builds and runs all benchmarks into results/*.csv; `python plot.py` makes the figures.
- README explains how to run it.
