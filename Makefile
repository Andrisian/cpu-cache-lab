# Release builds only: measurements must never come from debug builds.
CXX      := g++
CXXFLAGS := -O2 -std=c++17 -Wall -Wextra

.PHONY: run-sweep clean

build/working_set_sweep: bench/working_set_sweep.cpp
	mkdir -p build
	$(CXX) $(CXXFLAGS) -o $@ $<

# Build, then run the sweep and write results/working_set_sweep.csv
run-sweep: build/working_set_sweep
	mkdir -p results
	./build/working_set_sweep results/working_set_sweep.csv

clean:
	rm -rf build
