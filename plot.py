#!/usr/bin/env python3
"""Make the three figures from the benchmark CSVs.

Usage:  python plot.py [results_dir] [figures_dir]
        (defaults: results  figures)

Reads   results/working_set_sweep.csv, results/matrix_traversal.csv,
        results/array_vs_list.csv   (header: benchmark,pattern,size_bytes,ns_per_access)
Writes  figures/fig1_working_set_sweep.png
        figures/fig2_matrix_traversal.png
        figures/fig3_array_vs_list.png

Only values that are in the CSVs are plotted: one marker per measurement,
straight segments between neighbouring markers (a visual guide only, no
smoothing and no interpolated points). If a CSV is missing, that figure is
skipped and the others are still made.
"""

import csv
import os
import sys

import matplotlib
matplotlib.use("Agg")  # file output only, no GUI window
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, NullFormatter, NullLocator

# Cache sizes of THIS machine (Intel i7-8565U), from `lscpu --caches`.
# L1d and L2 are per core; the 8 MiB L3 is shared by all cores.
L1D_BYTES = 32 * 1024
L2_BYTES = 256 * 1024
L3_BYTES = 8 * 1024 * 1024
CACHES = [("L1d", L1D_BYTES), ("L2", L2_BYTES), ("L3", L3_BYTES)]

NODE_BYTES = 16     # list node: int64 value + 8-byte pointer
ELEMENT_BYTES = 8   # array element: int64

HEADER = ["benchmark", "pattern", "size_bytes", "ns_per_access"]
FOOTNOTE = "median of 7 runs, single run, CPU clock not controlled"


# ---------------------------------------------------------------- reading
def read_csv(path, benchmark):
    """Return {pattern: [(size_bytes, ns_per_access), ...]} sorted by size,
    or None (after printing why) if the file is missing or malformed."""
    if not os.path.exists(path):
        print("MISSING: %s not found - skipping its figure" % path)
        return None
    data = {}
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != HEADER:
            print("BAD HEADER in %s: expected %s, got %s - skipping its figure"
                  % (path, ",".join(HEADER), reader.fieldnames))
            return None
        for row in reader:
            if row["benchmark"] != benchmark:
                print("BAD ROW in %s: benchmark %r, expected %r - skipping its figure"
                      % (path, row["benchmark"], benchmark))
                return None
            data.setdefault(row["pattern"], []).append(
                (int(row["size_bytes"]), float(row["ns_per_access"])))
    for pattern in data:
        data[pattern].sort()
    return data


# ---------------------------------------------------------------- tick labels
def fmt_bytes(value, _pos):
    """4096 -> '4 KiB', 8388608 -> '8 MiB' (so no raw byte counts on axes)."""
    if value >= 2 ** 20:
        return "%g MiB" % (value / 2 ** 20)
    if value >= 2 ** 10:
        return "%g KiB" % (value / 2 ** 10)
    return "%g B" % value


def fmt_count(value, _pos):
    """1000 -> '1k', 3000000 -> '3M' (element counts)."""
    if value >= 1e6:
        return "%gM" % (value / 1e6)
    if value >= 1e3:
        return "%gk" % (value / 1e3)
    return "%g" % value


# ---------------------------------------------------------------- drawing
def make_figure(series, log_base, x_formatter, cache_lines, title, xlabel, notes, out_path):
    """series      : list of (label, [x...], [y...]) - the measured points
    cache_lines : list of (x_position, label) - dotted vertical lines
    notes       : extra footnote lines (the standard footnote is always added)"""
    fig, ax = plt.subplots(figsize=(9, 5.5))

    markers = ["o", "s", "^", "D"]
    all_x = []
    for i, (label, xs, ys) in enumerate(series):
        ax.plot(xs, ys, marker=markers[i % len(markers)], markersize=5,
                linewidth=1.2, label=label)
        all_x.extend(xs)

    ax.set_xscale("log", base=log_base)
    ax.set_yscale("log")

    # Tick exactly at the measured x values, labelled in KiB/MiB (or k/M).
    ticks = sorted(set(all_x))
    ax.set_xticks(ticks)
    ax.xaxis.set_major_formatter(FuncFormatter(x_formatter))
    ax.xaxis.set_minor_locator(NullLocator())
    ax.xaxis.set_minor_formatter(NullFormatter())
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right")

    # Dotted cache lines, only where they fall inside the plotted x range.
    x_min, x_max = min(all_x), max(all_x)
    for x, label in cache_lines:
        if x_min < x < x_max:
            ax.axvline(x, linestyle=":", color="gray", linewidth=1.2)
            ax.text(x, 0.98, label + " ", rotation=90, ha="right", va="top",
                    fontsize=8, color="dimgray", transform=ax.get_xaxis_transform())

    ax.set_xlabel(xlabel)
    ax.set_ylabel("ns per access")
    ax.grid(True, which="major", axis="y", alpha=0.3)

    # Legend above the plot area so it never covers data or cache-line labels.
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=len(series), frameon=False)
    ax.set_title(title, pad=30)

    lines = list(notes) + [FOOTNOTE]
    for k, text in enumerate(reversed(lines)):  # last line sits at the very bottom
        fig.text(0.01, 0.012 + 0.03 * k, text, fontsize=8, color="dimgray")
    fig.tight_layout(rect=[0, 0.03 * len(lines) + 0.01, 1, 1])

    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print("wrote %s" % out_path)


def as_series(data, wanted, x_divisor=1):
    """Pick patterns in the given order -> [(label, xs, ys)]; x = size_bytes/x_divisor.
    Returns None (after a message) if a wanted pattern is absent from the CSV."""
    out = []
    for pattern, label in wanted:
        if pattern not in data:
            print("MISSING pattern %r in CSV - skipping its figure" % pattern)
            return None
        pts = data[pattern]
        out.append((label, [p[0] // x_divisor for p in pts], [p[1] for p in pts]))
    return out


# ---------------------------------------------------------------- figures
def fig1(results_dir, figures_dir):
    data = read_csv(os.path.join(results_dir, "working_set_sweep.csv"), "working_set_sweep")
    if data is None:
        return
    series = as_series(data, [("sequential", "sequential read"),
                              ("random", "random pointer-chase")])
    if series is None:
        return
    make_figure(series, 2, fmt_bytes,
                [(b, "%s %s" % (name, fmt_bytes(b, None))) for name, b in CACHES],
                "Figure 1: time per access vs working-set size",
                "working set size (array size)", [],
                os.path.join(figures_dir, "fig1_working_set_sweep.png"))


def fig2(results_dir, figures_dir):
    data = read_csv(os.path.join(results_dir, "matrix_traversal.csv"), "matrix_traversal")
    if data is None:
        return
    series = as_series(data, [("row_major", "row-major"), ("column_major", "column-major")])
    if series is None:
        return
    notes = ["every point reads the same number of elements (2^24), so ns per access "
             "is proportional to total time"]
    make_figure(series, 2, fmt_bytes,
                [(b, "%s %s" % (name, fmt_bytes(b, None))) for name, b in CACHES],
                "Figure 2: summing an N x N matrix of doubles, row-major vs column-major",
                "matrix size (N x N x 8 bytes)", notes,
                os.path.join(figures_dir, "fig2_matrix_traversal.png"))


def fig3(results_dir, figures_dir):
    data = read_csv(os.path.join(results_dir, "array_vs_list.csv"), "array_vs_list")
    if data is None:
        return
    # x = element count n: array n = size_bytes/8, list n = size_bytes/16.
    array_pts = data.get("array")
    list_pts = data.get("list")
    if array_pts is None or list_pts is None:
        print("MISSING pattern 'array' or 'list' in CSV - skipping its figure")
        return
    series = [("array (contiguous)", [b // ELEMENT_BYTES for b, _ in array_pts],
               [ns for _, ns in array_pts]),
              ("list (random links)", [b // NODE_BYTES for b, _ in list_pts],
               [ns for _, ns in list_pts])]
    # Cache lines at the LIST's capacity in nodes: cache bytes / 16.
    # Two-line labels ("L3 8 MiB" / "524,288 nodes") are short enough to stay
    # above the data instead of running over the list line.
    lines = [(b // NODE_BYTES, "%s %s\n%s nodes" % (name, fmt_bytes(b, None),
                                                    "{:,}".format(b // NODE_BYTES)))
             for name, b in CACHES]
    notes = ["a list node is 16 bytes, an array element 8 bytes; dotted lines show each "
             "cache's capacity in list nodes (in array elements it is 2x larger)"]
    make_figure(series, 10, fmt_count, lines,
                "Figure 3: summing n int64 values, contiguous array vs randomly linked list",
                "number of elements n", notes,
                os.path.join(figures_dir, "fig3_array_vs_list.png"))


def main():
    results_dir = sys.argv[1] if len(sys.argv) > 1 else "results"
    figures_dir = sys.argv[2] if len(sys.argv) > 2 else "figures"
    os.makedirs(figures_dir, exist_ok=True)
    for make in (fig1, fig2, fig3):
        make(results_dir, figures_dir)


if __name__ == "__main__":
    main()
