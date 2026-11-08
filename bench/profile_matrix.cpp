// profile_matrix: a tiny program to run under `perf stat` (see profile.sh).
//
// Usage: ./build/profile_matrix <row|col|none> <passes>
//   none : only build and fill the 4096 x 4096 matrix of doubles. This is the
//          "setup-only" baseline: its counter values are subtracted from the
//          row/col runs so that only the traversal itself is left.
//   row  : fill, then sum the matrix row-major    for <passes> passes
//   col  : fill, then sum the matrix column-major for <passes> passes
//
// No timing here: perf measures. The kernels are copies of the ones in
// matrix_traversal.cpp (same loops, same asm barrier); the code is copied, not
// shared, on purpose.
//
// Build: g++ -O2 -std=c++17 -o build/profile_matrix bench/profile_matrix.cpp

#include <cstddef>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

constexpr std::size_t kN = 4096;  // 4096 * 4096 * 8 bytes = 128 MiB, far above L3

// A write to a volatile variable must really happen, so the compiler cannot
// conclude that the sum is unused and delete the loops (or the fill).
volatile double g_sink;

// Print the CPU model to stderr (project rule: CPU info at startup).
static void print_cpu_info() {
    std::ifstream cpuinfo("/proc/cpuinfo");
    std::string line;
    while (std::getline(cpuinfo, line)) {
        if (line.rfind("model name", 0) == 0) {
            std::cerr << "CPU: " << line.substr(line.find(':') + 2) << "\n";
            break;
        }
    }
}

// Row-major: inner loop runs along a row (consecutive addresses).
static double run_row_major(const double* a, std::size_t n, std::size_t passes) {
    double sum = 0.0;
    for (std::size_t p = 0; p < passes; ++p) {
        for (std::size_t r = 0; r < n; ++r)
            for (std::size_t c = 0; c < n; ++c) sum += a[r * n + c];
        // Empty asm that "may modify all memory": the matrix never changes, so
        // without it the compiler could compute one pass and reuse it.
        asm volatile("" : : "r"(a) : "memory");
    }
    return sum;
}

// Column-major: inner loop runs down a column (stride n*8 bytes).
static double run_column_major(const double* a, std::size_t n, std::size_t passes) {
    double sum = 0.0;
    for (std::size_t p = 0; p < passes; ++p) {
        for (std::size_t c = 0; c < n; ++c)
            for (std::size_t r = 0; r < n; ++r) sum += a[r * n + c];
        asm volatile("" : : "r"(a) : "memory");  // same barrier as above
    }
    return sum;
}

int main(int argc, char** argv) {
    std::string mode = (argc == 3) ? argv[1] : "";
    if (mode != "row" && mode != "col" && mode != "none") {
        std::cerr << "usage: " << argv[0] << " <row|col|none> <passes>\n";
        return 1;
    }
    std::size_t passes = (argc == 3) ? std::strtoull(argv[2], nullptr, 10) : 0;

    print_cpu_info();

    // Setup (also done in "none"): allocate and fill, same values as matrix_traversal.cpp.
    // Filling touches every page, so page faults happen here, not in the kernel.
    std::vector<double> m(kN * kN);
    for (std::size_t i = 0; i < m.size(); ++i) m[i] = 0.5 + static_cast<double>(i % 7) * 0.5;

    if (mode == "row") {
        g_sink = run_row_major(m.data(), kN, passes);
    } else if (mode == "col") {
        g_sink = run_column_major(m.data(), kN, passes);
    } else {
        g_sink = m[kN * kN / 2];  // "none": read one element so the fill is kept
    }
    return 0;
}
