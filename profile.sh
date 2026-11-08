#!/usr/bin/env bash
# Run ./build/profile_matrix under `perf stat` for: none (setup only), row, col.
# Pinned to CPU 2 with taskset, 8 passes each, 3 repetitions (-r 3).
#
# perf's own output (CSV, -x,) goes to results/profile_<kernel>.csv via -o, so
# the program's stderr cannot mix into it. This script only RUNS perf; every
# number comes from perf's output, nothing is computed or filled in here.
#
# Group 1 events: cycles,instructions,cache-references,cache-misses,branch-misses
# Group 2 events: L1-dcache-load-misses,dTLB-load-misses,LLC-load-misses
#                 (skipped, with a message, if perf prints <not supported>)
#
# /usr/bin/perf is a wrapper that needs a perf matching the running kernel
# (not installed here), so use the installed 5.15 binary directly. Override
# with:  PERF=/path/to/perf ./profile.sh
set -eu

PERF="${PERF:-/usr/lib/linux-tools/5.15.0-131-generic/perf}"
CPU=2
PASSES=8
REPS=3
BIN=./build/profile_matrix
GROUP1="cycles,instructions,cache-references,cache-misses,branch-misses"
GROUP2="L1-dcache-load-misses,dTLB-load-misses,LLC-load-misses"

mkdir -p results
echo "CPU: $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ //')"
echo "perf: $("$PERF" --version)"
echo "pinned to CPU $CPU, $PASSES passes, $REPS repetitions per kernel"

# run_group <name> <events> <file suffix> : one perf stat per kernel.
run_group() {
    local name="$1" events="$2" suffix="$3" k
    for k in none row col; do
        echo "[$name] $k ..."
        taskset -c "$CPU" "$PERF" stat -x, -r "$REPS" -e "$events" \
            -o "results/profile_${k}${suffix}.csv" "$BIN" "$k" "$PASSES" 2>/dev/null
    done
}

# Group 1
run_group "group 1" "$GROUP1" ""

# Group 2: run on 'none' first to find out whether the events are supported.
# If any line says <not supported> or <not counted>, say so and skip the group.
taskset -c "$CPU" "$PERF" stat -x, -e "$GROUP2" -o results/profile_probe_group2.csv \
    "$BIN" none 1 2>/dev/null || true
if grep -q -E '<not supported>|<not counted>' results/profile_probe_group2.csv; then
    echo "group 2 skipped: perf reports these events as not supported/not counted:"
    grep -E '<not supported>|<not counted>' results/profile_probe_group2.csv
else
    run_group "group 2" "$GROUP2" "_group2"
fi
rm -f results/profile_probe_group2.csv

echo "raw perf output in results/profile_{none,row,col}[_group2].csv"
