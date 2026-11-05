// array_vs_list: sum n int64 values stored two ways.
//   array : one contiguous std::vector<int64_t>, summed in order.
//   list  : nodes {value, next} linked in a RANDOM order that forms one cycle,
//           summed by following the next pointers.
//
// IMPORTANT: the "list" is NOT a std::list. All nodes sit in ONE contiguous
// std::vector<Node> (a pool), and only the LINKS are shuffled, using Sattolo's
// algorithm as in working_set_sweep.cpp. So the traversal order is random
// while the memory is still one block (no per-node `new`, no allocator effects).
//
// Both sums are int64_t (integer add, 1 cycle), so there is no floating-point
// add-latency confound between the two patterns.
//
// Build: g++ -O2 -std=c++17 -o build/array_vs_list bench/array_vs_list.cpp
// Run  : ./build/array_vs_list results/array_vs_list.csv
//        (with no argument the CSV goes to stdout)
//
// CSV columns: benchmark,pattern,size_bytes,ns_per_access
//   size_bytes = real footprint: n*8 for the array, n*sizeof(Node) for the list.
//   n itself is not a column: n = size_bytes/8 (array) or size_bytes/16 (list).
// CPU info and progress go to stderr so they never mix into the CSV.
// (Harness style copied from the other benchmarks on purpose, not shared.)

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
constexpr int kReps = 7;  // timed repetitions (median reported)

// Roughly equal work per point: passes = max(1, 2^24 / n). For n that does
// not divide 2^24 the total is slightly below 2^24; for n = 1e7 it is one
// pass of 1e7. ns/access is divided by the accesses actually done.
constexpr std::size_t kTargetAccesses = 1ull << 24;

// Element counts to sweep.
const std::size_t kCounts[] = {1000,   3000,   10000,   30000,  100000,
                               300000, 1000000, 3000000, 10000000};

// One list node: 16 bytes (8-byte value + 8-byte pointer on 64-bit Linux).
struct Node {
    std::int64_t value;
    Node* next;
};

// ---------------------------------------------------------------- sink
// A write to a volatile variable must really happen, so the compiler cannot
// conclude that the sum is unused and delete the measured loops.
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

// ---------------------------------------------------------------- data
// Small deterministic nonzero values, so sums are not trivially zero.
static std::int64_t value_for(std::size_t i) { return static_cast<std::int64_t>(i % 7) + 1; }

// Link the pool's nodes into ONE random cycle (Sattolo's algorithm).
// perm[i] = index of the node visited after node i. Sattolo is Fisher-Yates
// with the swap partner picked from [0, i-1] (never i), which forces a single
// cycle, so following next from any node visits all n nodes before returning.
// (A plain shuffle can split into short cycles and loop over only a few nodes.)
static void build_random_cycle(std::vector<Node>& pool, std::mt19937_64& rng) {
    std::size_t n = pool.size();
    std::vector<std::size_t> perm(n);
    std::iota(perm.begin(), perm.end(), 0);
    for (std::size_t i = n - 1; i > 0; --i) {
        std::uniform_int_distribution<std::size_t> pick(0, i - 1);
        std::swap(perm[i], perm[pick(rng)]);
    }
    for (std::size_t i = 0; i < n; ++i) {
        pool[i].value = value_for(i);
        pool[i].next = &pool[perm[i]];  // link to a random node of the same pool
    }
}  // perm is freed here

// ---------------------------------------------------------------- kernels
static std::size_t passes_for(std::size_t n) {
    return std::max<std::size_t>(1, kTargetAccesses / n);
}

// Array: add up all n elements, pass after pass.
static std::int64_t run_array(const std::int64_t* a, std::size_t n) {
    std::int64_t sum = 0;
    std::size_t passes = passes_for(n);
    for (std::size_t p = 0; p < passes; ++p) {
        for (std::size_t i = 0; i < n; ++i) sum += a[i];
        // Empty asm that "may modify all memory". The data never changes, so
        // without this the compiler may notice that every pass gives the same
        // sum and compute it once. The barrier forbids that.
        asm volatile("" : : "r"(a) : "memory");
    }
    return sum;
}

// List: follow next pointers for exactly n steps per pass, adding each value.
// The cycle brings us back to the start node after n steps.
static std::int64_t run_list(const Node* head, std::size_t n) {
    std::int64_t sum = 0;
    std::size_t passes = passes_for(n);
    for (std::size_t pass = 0; pass < passes; ++pass) {
        const Node* p = head;
        for (std::size_t i = 0; i < n; ++i) {
            sum += p->value;
            p = p->next;  // next address = value just loaded: a dependent chain
        }
        asm volatile("" : : "r"(head) : "memory");  // same barrier as the array
    }
    return sum;
}

// ---------------------------------------------------------------- harness
// 1 warm-up run (not timed), then kReps timed runs; returns the median ns/access.
// `kernel` is any callable returning the int64 sum.
template <class Kernel>
static double measure(Kernel kernel, std::size_t n) {
    g_sink = kernel();  // warm-up: fills caches, TLB, etc.

    // Accesses actually done per run.
    const double accesses = static_cast<double>(passes_for(n) * n);

    std::vector<double> ns_per_access;
    for (int r = 0; r < kReps; ++r) {
        auto t0 = std::chrono::steady_clock::now();
        std::int64_t result = kernel();
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

    std::mt19937_64 rng(12345);  // fixed seed -> same links every run

    for (std::size_t n : kCounts) {
        // Build both structures first (this also touches every page before timing).
        std::vector<std::int64_t> arr(n);
        for (std::size_t i = 0; i < n; ++i) arr[i] = value_for(i);

        std::vector<Node> pool(n);
        build_random_cycle(pool, rng);

        double a_ns = measure([&] { return run_array(arr.data(), n); }, n);
        double l_ns = measure([&] { return run_list(pool.data(), n); }, n);

        std::size_t a_bytes = n * sizeof(std::int64_t);
        std::size_t l_bytes = n * sizeof(Node);
        csv << "array_vs_list,array," << a_bytes << "," << a_ns << "\n";
        csv << "array_vs_list,list," << l_bytes << "," << l_ns << "\n";
        csv.flush();
        std::cerr << "n=" << n << ": array " << a_ns << " ns, list " << l_ns << " ns\n";
    }
    return 0;
}
