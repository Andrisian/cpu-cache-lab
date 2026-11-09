# Release builds only: measurements must never come from debug builds.
CXX      := g++
CXXFLAGS := -O2 -std=c++17 -Wall -Wextra

.PHONY: run cpuinfo run-sweep run-matrix run-list profile clean

build/working_set_sweep: bench/working_set_sweep.cpp
	mkdir -p build
	$(CXX) $(CXXFLAGS) -o $@ $<

# Build, then run the sweep and write results/working_set_sweep.csv
run-sweep: build/working_set_sweep
	mkdir -p results
	./build/working_set_sweep results/working_set_sweep.csv

build/matrix_traversal: bench/matrix_traversal.cpp
	mkdir -p build
	$(CXX) $(CXXFLAGS) -o $@ $<

# Build, then run the matrix sweep and write results/matrix_traversal.csv
run-matrix: build/matrix_traversal
	mkdir -p results
	./build/matrix_traversal results/matrix_traversal.csv

build/array_vs_list: bench/array_vs_list.cpp
	mkdir -p build
	$(CXX) $(CXXFLAGS) -o $@ $<

# Build, then run array vs list and write results/array_vs_list.csv
run-list: build/array_vs_list
	mkdir -p results
	./build/array_vs_list results/array_vs_list.csv

# Run all three benchmarks in order (writes all CSVs in results/).
# Do not use `make -j` here: timing runs must not overlap.
# cpuinfo comes first so results/cpu_info.txt always exists next to the CSVs.
run: cpuinfo run-sweep run-matrix run-list

# Save this machine's CPU model and cache sizes to results/cpu_info.txt, so the
# analysis agent can quote hardware facts from a file instead of from memory.
cpuinfo:
	mkdir -p results
	{ echo "# CPU model (lscpu):"; lscpu | grep -E '^Model name'; echo; echo "# lscpu --caches:"; lscpu --caches; } > results/cpu_info.txt

build/profile_matrix: bench/profile_matrix.cpp
	mkdir -p build
	$(CXX) $(CXXFLAGS) -o $@ $<

# Build, then run the perf stat profile (needs perf; see profile.sh).
# Writes results/profile_*.csv. Not part of `make run`.
profile: build/profile_matrix
	bash ./profile.sh

clean:
	rm -rf build
