# cpu-cache-lab

C++ micro-benchmarks that show CPU cache effects, a profiling run, and a small Python LLM agent that runs the benchmarks and analyzes the output. `SPEC.md` is the source of truth for what to build.

## Environment
- Ubuntu, g++, Python 3 with a virtualenv, matplotlib (pandas only if needed).
- Build benchmarks with `g++ -O2 -std=c++17`. Release builds only for measurements.
- Machine caches: check `lscpu --caches` and print CPU info at the start of every benchmark run.

## Working rules
- Work on one step or benchmark at a time. After each one, stop and summarize what changed and how to run it.
- Keep code simple and commented. Explain non-obvious choices in comments. No clever tricks the author cannot explain line by line.
- C++ benchmarks use only the standard library.
- Timing: `std::chrono::steady_clock`, one warm-up run, 7 repetitions, report the median.
- Prevent the compiler from deleting measured loops (volatile sink or an asm barrier).
- After each benchmark, list the measurement pitfalls you checked (optimization, warm-up, sizes vs cache sizes, access pattern).
- Never invent or estimate measurements. Report only numbers produced by actually running the code. If something was not run, say so.
- Do not edit `SPEC.md` without asking.
- Ask before installing system packages or running anything with sudo.
- Commit after each working step with a clear message.

## Analysis agent (later step)
- A human approves before the agent runs any tool.
- Log every tool call and its output.
- The agent may only state hardware facts that come from the CSV or profiler output, and must quote the numbers it relies on.
- Record cases where the agent is wrong or overclaims; they go into the README and the interview notes.

## Interview notes (update every step)
After finishing each step, append a section to `INTERVIEW_NOTES.md` (create it if missing) in this format:

```
### Step N: <name>
- Built: what we made, 1-2 lines
- Key concept: one sentence
- Likely questions: 3-5, each with a short answer
- Pitfalls found: what went wrong or could mislead
- Project story: what to say in an interview, with the real measured numbers from our runs
```

Keep it short and precise. Never include a result that was not actually measured. Keep `INTERVIEW_NOTES.md` out of git (listed in `.gitignore`) unless I say otherwise.
