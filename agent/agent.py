#!/usr/bin/env python3
"""Analysis agent for cpu-cache-lab.

Asks Claude to analyze the benchmark CSVs / perf output in results/. Claude can
call four tools (list_results, read_csv, run_benchmark, run_profile). A HUMAN
must type "y" before ANY tool runs, and every tool call is logged.

Usage:
    export ANTHROPIC_API_KEY=...            # the only place the key is read from
    .venv/bin/python agent/agent.py "Compare row-major and column-major"
    .venv/bin/python agent/agent.py --max-steps 6 --max-tokens 50000 "question"
    AGENT_MODEL=claude-haiku-5-5 ...        # model (this is the default)

Exit codes: 0 finished, 1 API/setup error, 2 stopped by a limit or a non-normal
stop reason, 130 interrupted with Ctrl-C.

Log: logs/agent_log.jsonl (one JSON object per line; the API key is never logged).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import anthropic

# ---------------------------------------------------------------- settings
DEFAULT_MODEL = "claude-haiku-5-5"  # override with the AGENT_MODEL environment variable

# ===== PRICES (US dollars per 1,000,000 tokens) - CHECK THESE YOURSELF =====
# Copied from the claude-api skill's cached price table (dated 2026-10-06) for
# claude-haiku-5-5, prompts up to 100K tokens (above that the table lists
# $0.50 input / $2.50 output). NOT verified against Anthropic's pricing page.
# The cost shown is an ESTIMATE: input_tokens and output_tokens from
# response.usage times these prices. (Thinking tokens count as output tokens.
# No prompt caching is used, so cache prices are ignored.)
PRICE_MODEL = "claude-haiku-5-5"
PRICE_INPUT_PER_MTOK = 0.10
PRICE_OUTPUT_PER_MTOK = 0.50
# ===========================================================================

MAX_TOKENS_PER_CALL = 8000      # output cap of one API call (thinking + answer)
DEFAULT_MAX_STEPS = 10          # max API calls per run
DEFAULT_TOKEN_BUDGET = 100000   # max input+output tokens summed over all calls
MAX_TOOL_OUTPUT_CHARS = 8000    # longer tool output is cut before the model sees it

ROOT = Path(__file__).resolve().parent.parent  # project root (parent of agent/)
RESULTS_DIR = ROOT / "results"
LOG_PATH = ROOT / "logs" / "agent_log.jsonl"

SYSTEM_PROMPT = """You are a careful performance-analysis assistant for the cpu-cache-lab project (C++ cache micro-benchmarks and a perf stat profile). You can only learn about this machine through your tools. A human approves every tool call first; a denial means you get no data.

Rules (follow them strictly):
1. State a hardware or performance fact ONLY if it comes from CSV or profiler output returned by a tool in this conversation. Quote the exact numbers you rely on, with the file name (and the pattern/size or perf event) each number came from.
2. For anything else (cache sizes, CPU model, clock speed, core counts, what a perf event counts, ...) write "not measured". Do not fill such facts in from memory. Do not guess cache sizes or CPU specs.
3. You may do arithmetic on quoted numbers; label it "derived" and show the calculation.
4. Do not claim a cause unless the numbers show it. Give causes as hypotheses and say they were not tested.
5. The data are single runs with an uncontrolled CPU clock. Say so when comparing close numbers and do not over-read small differences.
6. results/ can contain older or extra files; say which file each number came from. Do not run a benchmark or the profile unless you need data you do not have: running one overwrites existing results.
7. If the data needed to answer is missing, say what is missing instead of answering.
Keep answers short and precise.

File guide (project layout, not a measurement): benchmark CSVs have the header benchmark,pattern,size_bytes,ns_per_access. profile_*.csv files are raw `perf stat -x,` output (columns: value, unit, event, variance, run time in ns, percent of time counted, ...); profile_none.csv is the setup-only baseline (matrix fill, no traversal), so a kernel's own counts are profile_row or profile_col minus profile_none."""


# ---------------------------------------------------------------- secrets
def redact(text: str) -> str:
    """Remove the API key (and anything shaped like one) from text before it
    is printed or logged."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        text = text.replace(key, "[REDACTED]")
    return re.sub(r"sk-ant-[A-Za-z0-9_\-]+", "[REDACTED]", text)


def say(text: str = "") -> None:
    """print() that always redacts."""
    print(redact(text))


# ---------------------------------------------------------------- logging
class RunLog:
    """Append-only JSON-lines log. One line per event, with run id and time."""

    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id

    def write(self, event: str, **fields: Any) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(),
                  "run_id": self.run_id, "event": event}
        record.update(fields)
        line = redact(json.dumps(record, ensure_ascii=False, default=str))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path), "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------------------------------------------------------------- tools
# Each tool function takes the model's arguments (a dict) and returns
# (output_text, is_error). They never raise.

def safe_results_path(name: Any) -> Path:
    """Map a file name from the model to a path inside results/, or raise
    ValueError. Rejects empty names, absolute paths, '..', backslashes and
    non-.csv names; then resolves symlinks and checks the real path is still
    inside results/."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    if "\x00" in name or "\\" in name:
        raise ValueError("invalid characters in name")
    if name.startswith("/") or os.path.isabs(name) or re.match(r"^[A-Za-z]:", name):
        raise ValueError("absolute paths are not allowed")
    if ".." in name:
        raise ValueError("'..' is not allowed")
    if not name.endswith(".csv"):
        raise ValueError("only .csv files can be read")
    results_real = RESULTS_DIR.resolve()
    path = (RESULTS_DIR / name).resolve()  # follows symlinks
    try:
        path.relative_to(results_real)
    except ValueError:
        raise ValueError("path is outside results/")
    return path


def tool_list_results(args: Dict[str, Any]) -> Tuple[str, bool]:
    if not RESULTS_DIR.is_dir():
        return "results/ does not exist (no benchmark has been run yet)", True
    names = sorted(p.name for p in RESULTS_DIR.glob("*.csv"))
    return ("\n".join(names) if names else "(no CSV files in results/)"), False


def tool_read_csv(args: Dict[str, Any]) -> Tuple[str, bool]:
    try:
        path = safe_results_path(args.get("name"))
    except ValueError as e:
        return "rejected: %s" % e, True
    if not path.is_file():
        return "file not found: results/%s" % args.get("name"), True
    try:
        return path.read_text(encoding="utf-8", errors="replace"), False
    except OSError as e:
        return "could not read file: %s" % e, True


def run_command(cmd: List[str], timeout: int) -> Tuple[str, bool]:
    """Run a fixed command (no shell) in the project root; return its output
    and exit code as text."""
    shown = " ".join(cmd)
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, universal_newlines=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return "$ %s\n[timed out after %d s]" % (shown, timeout), True
    except OSError as e:
        return "$ %s\n[could not start: %s]" % (shown, e), True
    return "$ %s\n%s\n[exit code %d]" % (shown, proc.stdout, proc.returncode), proc.returncode != 0


# The only commands the agent can ever start: name -> (make target, CSV it overwrites).
BENCHMARKS = {
    "run-sweep": "working_set_sweep.csv",
    "run-matrix": "matrix_traversal.csv",
    "run-list": "array_vs_list.csv",
}


def tool_run_benchmark(args: Dict[str, Any]) -> Tuple[str, bool]:
    name = args.get("name")
    if name not in BENCHMARKS:
        return "rejected: name must be one of %s" % ", ".join(sorted(BENCHMARKS)), True
    return run_command(["make", name], timeout=600)


def tool_run_profile(args: Dict[str, Any]) -> Tuple[str, bool]:
    return run_command(["bash", "./profile.sh"], timeout=300)


def preview_for(name: str, args: Dict[str, Any]) -> str:
    """One line telling the human exactly what approving will do."""
    if name == "list_results":
        return "list the CSV files in results/"
    if name == "read_csv":
        return "read results/%s" % args.get("name")
    if name == "run_benchmark":
        target = args.get("name")
        csv_name = BENCHMARKS.get(target, "?")
        return "RUN: make %s   (overwrites results/%s)" % (target, csv_name)
    if name == "run_profile":
        return "RUN: bash ./profile.sh   (overwrites results/profile_*.csv)"
    return "unknown tool"


def schema(properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


TOOLS = {
    "list_results": {
        "description": "List the CSV files in the results/ directory (file names only).",
        "input_schema": schema({}, []),
        "func": tool_list_results,
    },
    "read_csv": {
        "description": ("Return the text of one CSV file in results/. 'name' is a file name "
                        "such as 'matrix_traversal.csv'. Paths, '..' and non-.csv names are rejected."),
        "input_schema": schema(
            {"name": {"type": "string", "description": "CSV file name inside results/"}}, ["name"]),
        "func": tool_read_csv,
    },
    "run_benchmark": {
        "description": ("Run one benchmark with make (run-sweep, run-matrix or run-list). It OVERWRITES "
                        "the matching results/*.csv and may take a minute or more. Use only if the "
                        "needed data is missing."),
        "input_schema": schema(
            {"name": {"type": "string", "enum": sorted(BENCHMARKS)}}, ["name"]),
        "func": tool_run_benchmark,
    },
    "run_profile": {
        "description": ("Run ./profile.sh (perf stat for the setup-only, row-major and column-major "
                        "matrix runs). Overwrites results/profile_*.csv, returns the script output, "
                        "takes tens of seconds."),
        "input_schema": schema({}, []),
        "func": tool_run_profile,
    },
}


def api_tools() -> List[Dict[str, Any]]:
    """Tool definitions in the format the API expects."""
    return [{"name": n, "description": t["description"], "input_schema": t["input_schema"]}
            for n, t in TOOLS.items()]


def truncate_for_model(text: str) -> Tuple[str, bool]:
    """Cut long output before sending it to the model and SAY it was cut."""
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text, False
    note = "\n[truncated: showing %d of %d characters]" % (MAX_TOOL_OUTPUT_CHARS, len(text))
    return text[:MAX_TOOL_OUTPUT_CHARS] + note, True


# ---------------------------------------------------------------- approval
def ask_approval(name: str, args: Dict[str, Any], input_fn: Callable[[str], str]) -> bool:
    """Show the tool call and require the human to type exactly 'y'."""
    say("")
    say("---- tool request ----")
    say("tool:      %s" % name)
    say("arguments: %s" % json.dumps(args, ensure_ascii=False))
    say("effect:    %s" % preview_for(name, args))
    try:
        answer = input_fn("Type y to approve (anything else denies): ")
    except EOFError:
        say("(no input available: denied)")
        return False
    return answer.strip() == "y"


def execute_tool(name: str, args: Any, log: RunLog, input_fn: Callable[[str], str]) -> Tuple[str, bool]:
    """Ask for approval, run the tool if approved, log everything, and return
    (text for the model, is_error)."""
    if not isinstance(args, dict):
        args = {}
    if name not in TOOLS:
        # Not one of our tools: nothing to approve or run.
        output = "unknown tool: %s" % name
        log.write("tool_call", tool=name, arguments=args, approved=None, output=output)
        return output, True

    approved = ask_approval(name, args, input_fn)
    if not approved:
        output = "denied by user"
        log.write("tool_call", tool=name, arguments=args, approved=False, output=output)
        say("denied.")
        return output, True

    start = time.monotonic()
    output, is_error = TOOLS[name]["func"](args)
    duration = round(time.monotonic() - start, 3)
    sent, truncated = truncate_for_model(output)
    # The log keeps the FULL output, even if the model only sees a cut version.
    log.write("tool_call", tool=name, arguments=args, approved=True, is_error=is_error,
              duration_s=duration, output=output, truncated_for_model=truncated)
    say("tool finished in %.1f s%s" % (duration, " (output truncated for the model)" if truncated else ""))
    return sent, is_error


# ---------------------------------------------------------------- cost
def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1e6) * PRICE_INPUT_PER_MTOK + (output_tokens / 1e6) * PRICE_OUTPUT_PER_MTOK


# ---------------------------------------------------------------- API errors
def describe_api_error(e: Exception) -> str:
    """A clear one-line explanation for the errors we expect."""
    msg = redact(str(getattr(e, "message", e)))
    if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return "API key problem (%s): check ANTHROPIC_API_KEY and its permissions. Detail: %s" % (
            type(e).__name__, msg)
    if isinstance(e, anthropic.BadRequestError):
        low = msg.lower()
        if "credit" in low or "billing" in low or "balance" in low:
            return ("Billing/credit problem: your prepaid balance may be used up or not active. "
                    "Check Plans & Billing in the Anthropic Console. Detail: %s" % msg)
        return "Bad request (the API rejected the request): %s" % msg
    if isinstance(e, anthropic.RateLimitError):
        return "Rate limit reached (HTTP 429). Wait a bit and run again. Detail: %s" % msg
    if isinstance(e, anthropic.APITimeoutError):
        return "The request to the API timed out. Check your network and run again."
    if isinstance(e, anthropic.APIConnectionError):
        return "Network error: could not reach the API. Check your internet connection."
    if isinstance(e, anthropic.APIStatusError):
        return "API error (HTTP %s): %s" % (e.status_code, msg)
    return "Unexpected API error (%s): %s" % (type(e).__name__, msg)


# ---------------------------------------------------------------- the loop
def text_of(content: Any) -> str:
    """Join the text blocks of a response."""
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text")


def run_agent(client: Any, model: str, question: str, max_steps: int, token_budget: int,
              log: RunLog, input_fn: Callable[[str], str] = input) -> int:
    """Tool-calling loop. Returns the process exit code."""
    messages = [{"role": "user", "content": question}]
    total_in = total_out = 0
    log.write("run_start", model=model, max_steps=max_steps, token_budget=token_budget,
              question=question)

    for step in range(1, max_steps + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS_PER_CALL,
                system=SYSTEM_PROMPT,
                tools=api_tools(),
                messages=messages,
            )
        except anthropic.APIError as e:
            text = describe_api_error(e)
            say("\nSTOPPED: " + text)
            log.write("api_error", step=step, error_class=type(e).__name__,
                      status_code=getattr(e, "status_code", None), message=text)
            return 1

        # Token accounting (from response.usage) and running cost estimate.
        in_tok, out_tok = response.usage.input_tokens, response.usage.output_tokens
        total_in += in_tok
        total_out += out_tok
        cost = estimate_cost(total_in, total_out)
        say("[step %d] in=%d out=%d | total in=%d out=%d (%d) | est. cost so far $%.5f" % (
            step, in_tok, out_tok, total_in, total_out, total_in + total_out, cost))
        text = text_of(response.content)
        log.write("api_call", step=step, input_tokens=in_tok, output_tokens=out_tok,
                  total_input_tokens=total_in, total_output_tokens=total_out,
                  estimated_cost_usd=round(cost, 6), stop_reason=response.stop_reason,
                  request_id=getattr(response, "_request_id", None), text=text)

        if total_in + total_out >= token_budget:
            if text:
                say("\n[agent says]\n" + text)
            say("\nSTOPPED: token budget reached (%d of %d). No further tools were run." % (
                total_in + total_out, token_budget))
            log.write("stopped", reason="token_budget", total_tokens=total_in + total_out)
            return 2

        # Pass the assistant turn back unchanged (the API needs it for the tool results).
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "tool_use":
            if text:
                say("\n[agent says]\n" + text)
            results = []
            for block in response.content:
                if getattr(block, "type", None) == "tool_use":
                    output, is_error = execute_tool(block.name, block.input, log, input_fn)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": output, "is_error": is_error})
            # All results of one assistant turn go back in ONE user message.
            messages.append({"role": "user", "content": results})
            continue

        # Anything else ends the run: normal end_turn, or max_tokens / refusal / ...
        say("\n[final answer]\n" + (text or "(no text)"))
        log.write("final", stop_reason=response.stop_reason, text=text,
                  total_tokens=total_in + total_out, estimated_cost_usd=round(cost, 6))
        if response.stop_reason != "end_turn":
            say("\nNOTE: the model stopped with stop_reason=%s, so the answer may be incomplete." %
                response.stop_reason)
            return 2
        return 0

    say("\nSTOPPED: reached the maximum of %d steps without a final answer." % max_steps)
    log.write("stopped", reason="max_steps", total_tokens=total_in + total_out)
    return 2


# ---------------------------------------------------------------- main
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Analysis agent for the cpu-cache-lab results.")
    parser.add_argument("question", nargs="*", help="what to analyze (asked interactively if omitted)")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS,
                        help="maximum API calls (default %d)" % DEFAULT_MAX_STEPS)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_TOKEN_BUDGET,
                        help="total input+output token budget over all calls (default %d)" % DEFAULT_TOKEN_BUDGET)
    args = parser.parse_args(argv)
    if args.max_steps < 1 or args.max_tokens < 1000:
        parser.error("--max-steps must be >= 1 and --max-tokens >= 1000")

    key = os.environ.get("ANTHROPIC_API_KEY")  # the ONLY source of the key; never printed
    if not key:
        say("ANTHROPIC_API_KEY is not set. Export it in your shell, then run again.")
        return 1
    model = os.environ.get("AGENT_MODEL", DEFAULT_MODEL)
    client = anthropic.Anthropic(api_key=key)

    try:
        question = " ".join(args.question).strip() or input("Question: ").strip()
    except (EOFError, KeyboardInterrupt):
        say("\nno question given.")
        return 1
    if not question:
        say("no question given.")
        return 1

    run_id = uuid.uuid4().hex[:8]
    log = RunLog(LOG_PATH, run_id)
    say("model: %s | max steps: %d | token budget: %d | log: %s" % (
        model, args.max_steps, args.max_tokens, LOG_PATH))
    if model != PRICE_MODEL:
        say("WARNING: the price constants are for %s, so the cost estimate is NOT valid for %s." % (
            PRICE_MODEL, model))
    say("Every tool call needs your approval (type y).")

    try:
        return run_agent(client, model, question, args.max_steps, args.max_tokens, log)
    except KeyboardInterrupt:
        say("\ninterrupted.")
        log.write("stopped", reason="keyboard_interrupt")
        return 130


if __name__ == "__main__":
    sys.exit(main())
