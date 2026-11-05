// matrix_traversal: sum an N x N matrix of doubles in two orders.
//
// The matrix is stored row after row in memory (row-major), so element
// (r, c) lives at a[r*N + c].
//   row_major    : r outer, c inner -> walks memory in order, stride 8 bytes.
//                  Each 64-byte cache line (8 doubles) is fully used.
//   column_major : c outer, r inner -> jumps N*8 bytes per access.
//                  For big N every access lands on a different cache line,
//                  and we use only 8 of its 64 bytes before it is evicted.
//
// Build: g++ -O2 -std=c++17 -o build/matrix_traversal bench/matrix_traversal.cpp
// Run  : ./build/matrix_traversal results/matrix_traversal.csv
//        (with no argument the CSV goes to stdout)
//
// CSV columns: benchmark,pattern,size_bytes,ns_per_access
//   size_bytes = N*N*8 (the matrix size); ns_per_access = ns per element read.
// CPU info and progress go to stderr so they never mix into the CSV.
// (Harness style copied from working_set_sweep.cpp on purpose, not shared.)

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

// ---------------------------------------------------------------- settings
constexpr std::size_t kMinN = 256;
constexpr std::size_t kMaxN = 4096;  // 4096*4096*8 B = 128 MiB, far above L3
constexpr int kReps = 7;             // timed repetitions (median reported)

// Same total work at every N, so the timer always runs long enough and
// ns/access is comparable across sizes. 2^24 = 16.7M element reads, which is
// exactly one full pass at N = 4096. Smaller N repeat the matrix several times.
constexpr std::size_t kAccesses = 1ull << 24;

// ---------------------------------------------------------------- sink
// A write to a volatile variable must really happen, so the compiler cannot
// conclude that the sum is unused and delete the measured loops.
volatile double g_sink;

// ---------------------------------------------------------------- CPU info
// Only the standard library is allowed, so read Linux's text files directly
// instead of running `lscpu`.
static std::string read_first_line(const std::string& path) {
    std::ifstream f(path);
    std::string line;
    std::getline(f, line);
    return line;
}

static void print_cpu_info() {
    std::ifstream cpuinfo("/proc/cpuinfo");
    std::string line;
    while (std::getline(cpuinfo, line)) {
        if (line.rfind("model name", 0) == 0) {  // line starts with "model name"
            std::cerr << "CPU: " << line.substr(line.find(':') + 2) << "\n";
            break;
        }
    }
    // cpu0's caches: index0, index1, ... These sizes are per core
    // (L3 is shared by all cores).
    for (int i = 0;; ++i) {
        std::string dir = "/sys/devices/system/cpu/cpu0/cache/index" + std::to_string(i) + "/";
        std::string size = read_first_line(dir + "size");
        if (size.empty()) break;  // no more cache levels
        std::cerr << "  L" << read_first_line(dir + "level") << " "
                  << read_first_line(dir + "type") << ": " << size << "\n";
    }
}

// ---------------------------------------------------------------- kernels
// Number of full passes over the matrix so that every N does kAccesses reads.
static std::size_t passes_for(std::size_t n) {
    return std::max<std::size_t>(1, kAccesses / (n * n));
}

// Row-major: inner loop runs along a row (consecutive addresses).
static double run_row_major(const double* a, std::size_t n) {
    double sum = 0.0;
    std::size_t passes = passes_for(n);
    for (std::size_t p = 0; p < passes; ++p) {
        for (std::size_t r = 0; r < n; ++r)
            for (std::size_t c = 0; c < n; ++c) sum += a[r * n + c];
        // Empty asm that "may modify all memory". The matrix never changes, so
        // without this the compiler may notice every pass gives the same sum
        // and compute it once. The barrier forbids that: after it, the
        // compiler must assume the data could be different, so it must read
        // it again.
        asm volatile("" : : "r"(a) : "memory");
    }
    return sum;
}

// Column-major: inner loop runs down a column (stride n*8 bytes).
static double run_column_major(const double* a, std::size_t n) {
    double sum = 0.0;
    std::size_t passes = passes_for(n);
    for (std::size_t p = 0; p < passes; ++p) {
        for (std::size_t c = 0; c < n; ++c)
            for (std::size_t r = 0; r < n; ++r) sum += a[r * n + c];
        asm volatile("" : : "r"(a) : "memory");  // same barrier as above
    }
    return sum;
}

// ---------------------------------------------------------------- harness
using Kernel = double (*)(const double*, std::size_t);

// 1 warm-up run (not timed), then kReps timed runs; returns the median ns/access.
static double measure(Kernel kernel, const std::vector<double>& m, std::size_t n) {
    g_sink = kernel(m.data(), n);  // warm-up: fills caches, TLB, etc.

    // Reads actually done per run = passes * n * n (equals kAccesses here
    // because every n is a power of two <= 4096).
    const double accesses = static_cast<double>(passes_for(n) * n * n);

    std::vector<double> ns_per_access;
    for (int r = 0; r < kReps; ++r) {
        auto t0 = std::chrono::steady_clock::now();
        double result = kernel(m.data(), n);
        auto t1 = std::chrono::steady_clock::now();
        g_sink = result;  // volatile write keeps the work alive
        double ns = std::chrono::duration<double, std::nano>(t1 - t0).count();
        ns_per_access.push_back(ns / accesses);
    }
    std::sort(ns_per_access.begin(), ns_per_access.end());
    return ns_per_access[kReps / 2];  // middle of 7 = median
}

int main(int argc, char** argv) {
    print_cpu_info();

    // CSV goes to the file named in argv[1], or to stdout if none is given.
    std::ofstream file;
    if (argc > 1) {
        file.open(argv[1]);
        if (!file) {
            std::cerr << "cannot open " << argv[1] << "\n";
            return 1;
        }
    }
    std::ostream& csv = (argc > 1) ? static_cast<std::ostream&>(file) : std::cout;
    csv << "benchmark,pattern,size_bytes,ns_per_access\n";

    for (std::size_t n = kMinN; n <= kMaxN; n *= 2) {
        // Fill with small deterministic nonzero values (0.5 .. 3.5) so the
        // sums are not trivially zero. Filling also touches every page
        // before timing, so page faults are not measured.
        std::vector<double> m(n * n);
        for (std::size_t i = 0; i < m.size(); ++i) m[i] = 0.5 + static_cast<double>(i % 7) * 0.5;

        double row = measure(run_row_major, m, n);
        double col = measure(run_column_major, m, n);

        std::size_t bytes = n * n * sizeof(double);
        csv << "matrix_traversal,row_major," << bytes << "," << row << "\n";
        csv << "matrix_traversal,column_major," << bytes << "," << col << "\n";
        csv.flush();
        std::cerr << "N=" << n << " (" << bytes / 1024 << " KiB): row " << row
                  << " ns, column " << col << " ns\n";
    }
    return 0;
}
