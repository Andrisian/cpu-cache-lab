// working_set_sweep: how does time per memory access change with the size of
// the data we touch?  Small arrays fit in L1/L2/L3 cache and are fast; big
// arrays spill to RAM and are slow.
//
// Two access patterns over an int64 array:
//   sequential : read elements 0,1,2,... in order (cache + prefetcher friendly)
//   random     : pointer-chase through a random single-cycle permutation
//                (every load depends on the previous one, so nothing can be
//                 overlapped or prefetched -> measures real latency)
//
// Build: g++ -O2 -std=c++17 -o build/working_set_sweep bench/working_set_sweep.cpp
// Run  : ./build/working_set_sweep results/working_set_sweep.csv
//        (with no argument the CSV goes to stdout)
//
// CSV columns: benchmark,pattern,size_bytes,ns_per_access
// CPU info and progress go to stderr so they never mix into the CSV.

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <numeric>
#include <random>
#include <string>
#include <vector>

// ---------------------------------------------------------------- settings
constexpr std::size_t kMinBytes = 4ull << 10;    // 4 KiB
constexpr std::size_t kMaxBytes = 256ull << 20;  // 256 MiB
constexpr int kReps = 7;                         // timed repetitions (median reported)

// Every measurement does the same number of accesses, whatever the array size,
// so the timer always runs long enough (tens of ms even for 4 KiB) and the
// ns/access numbers are directly comparable. 2^25 = 33.5M accesses, which is
// exactly one full pass over the largest array (256 MiB / 8 bytes).
constexpr std::size_t kAccesses = 1ull << 25;

// ---------------------------------------------------------------- sink
// Anything written to a volatile variable must really happen, so the compiler
// cannot decide "nobody uses this result" and delete the measured loop.
volatile std::int64_t g_sink;

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
    // cpu0's caches: index0, index1, ... (L1d, L1i, L2, L3 on most machines).
    // These sizes are per core (L3 is shared by all cores).
    for (int i = 0;; ++i) {
        std::string dir = "/sys/devices/system/cpu/cpu0/cache/index" + std::to_string(i) + "/";
        std::string size = read_first_line(dir + "size");
        if (size.empty()) break;  // no more cache levels
        std::cerr << "  L" << read_first_line(dir + "level") << " "
                  << read_first_line(dir + "type") << ": " << size << "\n";
    }
}

// ---------------------------------------------------------------- patterns
// Build a random permutation with ONE cycle covering all n slots (Sattolo's
// algorithm). arr[i] = "the next index to visit after i". Because it is a
// single cycle, following arr[] from any start visits every element exactly
// once before returning, so the whole array is the working set.
// (A plain shuffle can split into several short cycles, which would loop over
//  only a small part of the array and fake a smaller working set.)
static void build_single_cycle(std::vector<std::int64_t>& arr, std::mt19937_64& rng) {
    std::size_t n = arr.size();
    std::iota(arr.begin(), arr.end(), 0);  // arr = 0,1,2,...,n-1
    // Sattolo: like Fisher-Yates but j is in [0, i-1] (never i), which forces
    // a single cycle.
    for (std::size_t i = n - 1; i > 0; --i) {
        std::uniform_int_distribution<std::size_t> pick(0, i - 1);
        std::swap(arr[i], arr[pick(rng)]);
    }
}

// Sequential read: add up all elements, pass after pass, kAccesses in total.
static std::int64_t run_sequential(const std::int64_t* a, std::size_t n) {
    std::int64_t sum = 0;
    std::size_t passes = std::max<std::size_t>(1, kAccesses / n);
    for (std::size_t p = 0; p < passes; ++p) {
        for (std::size_t i = 0; i < n; ++i) sum += a[i];
        // Empty asm that "may modify all memory". The array never changes, so
        // without this the compiler is allowed to notice that every pass gives
        // the same sum and compute it once. The barrier forbids that.
        asm volatile("" : : "r"(a) : "memory");
    }
    return sum;
}

// Random pointer-chase: idx = a[idx], kAccesses times. The next address is
// the value just loaded, so the CPU must wait for each load to finish.
static std::int64_t run_random(const std::int64_t* a, std::size_t /*n*/) {
    std::int64_t idx = 0;
    for (std::size_t k = 0; k < kAccesses; ++k) idx = a[idx];
    return idx;  // final index depends on every load, so none can be dropped
}

// ---------------------------------------------------------------- harness
using Kernel = std::int64_t (*)(const std::int64_t*, std::size_t);

// 1 warm-up run (not timed), then kReps timed runs; returns the median ns/access.
static double measure(Kernel kernel, const std::vector<std::int64_t>& arr) {
    g_sink = kernel(arr.data(), arr.size());  // warm-up: fills caches, TLB, etc.

    std::vector<double> ns_per_access;
    for (int r = 0; r < kReps; ++r) {
        auto t0 = std::chrono::steady_clock::now();
        std::int64_t result = kernel(arr.data(), arr.size());
        auto t1 = std::chrono::steady_clock::now();
        g_sink = result;  // volatile write keeps the work alive
        double ns = std::chrono::duration<double, std::nano>(t1 - t0).count();
        ns_per_access.push_back(ns / static_cast<double>(kAccesses));
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

    std::mt19937_64 rng(12345);  // fixed seed -> same permutation every run

    for (std::size_t bytes = kMinBytes; bytes <= kMaxBytes; bytes *= 2) {
        std::vector<std::int64_t> arr(bytes / sizeof(std::int64_t));
        build_single_cycle(arr, rng);  // also touches every page before timing

        double seq = measure(run_sequential, arr);
        double rnd = measure(run_random, arr);

        csv << "working_set_sweep,sequential," << bytes << "," << seq << "\n";
        csv << "working_set_sweep,random," << bytes << "," << rnd << "\n";
        csv.flush();
        std::cerr << bytes / 1024 << " KiB: seq " << seq << " ns, random " << rnd << " ns\n";
    }
    return 0;
}
