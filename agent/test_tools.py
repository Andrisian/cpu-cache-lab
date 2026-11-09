#!/usr/bin/env python3
"""Offline checks for agent.py. No API calls, no network, no benchmark runs.

Run:  .venv/bin/python agent/test_tools.py
Plain asserts, no pytest. Uses a fake client (canned responses) for the loop.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx  # installed together with anthropic; used only to build error objects
import anthropic

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent  # noqa: E402  (agent/agent.py)

FAKE_KEY = "sk-ant-api03-FAKEKEYFORTESTSONLY"
os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY  # fake value, only to test redaction


# ---------------------------------------------------------------- helpers
def quiet(fn, *a, **kw):
    """Run fn with stdout captured; return (result, captured_text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*a, **kw)
    return result, buf.getvalue()


def usage(i, o):
    return SimpleNamespace(input_tokens=i, output_tokens=o)


def reply_text(text, i=100, o=50):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                           stop_reason="end_turn", usage=usage(i, o))


def reply_tool(tool, args, i=100, o=50):
    block = SimpleNamespace(type="tool_use", id="tu_1", name=tool, input=args)
    return SimpleNamespace(content=[block], stop_reason="tool_use", usage=usage(i, o))


class FakeClient:
    """Stands in for anthropic.Anthropic: returns canned responses or raises."""

    def __init__(self, items):
        self.items = list(items)
        self.calls = 0
        self.seen = []  # copy of the messages list at each call
        self.messages = self  # so client.messages.create(...) works

    def create(self, **kwargs):
        self.calls += 1
        self.seen.append(list(kwargs["messages"]))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_log(tmp):
    return agent.RunLog(Path(tmp) / "log.jsonl", "test")


def log_events(log):
    return [json.loads(line) for line in log.path.read_text().splitlines()]


def must_not_run(*a, **kw):
    raise AssertionError("subprocess.run was called but nothing should have executed")


# ---------------------------------------------------------------- tests
def test_path_restriction():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        results = tmp / "results"
        results.mkdir()
        (results / "real.csv").write_text("a,b\n1,2\n")
        (tmp / "secret.csv").write_text("secret\n")
        (results / "link.csv").symlink_to(tmp / "secret.csv")          # symlink escape
        (results / "linkdir").symlink_to(tmp, target_is_directory=True)  # directory escape
        agent.RESULTS_DIR = results

        assert agent.tool_read_csv({"name": "real.csv"}) == ("a,b\n1,2\n", False)
        for bad in ["", "   ", None, 5, "../secret.csv", "/etc/passwd", "a/../real.csv",
                    "..", "real.txt", "C:\\x.csv", "sub\\real.csv", "link.csv",
                    "linkdir/secret.csv", "real.csv\x00"]:
            out, err = agent.tool_read_csv({"name": bad})
            assert err and out.startswith("rejected"), (bad, out)
        out, err = agent.tool_read_csv({"name": "missing.csv"})
        assert err and "not found" in out
        listing, err = agent.tool_list_results({})
        assert not err and "real.csv" in listing


def test_truncation():
    short, cut = agent.truncate_for_model("x" * 10)
    assert short == "x" * 10 and not cut
    long_text = "y" * (agent.MAX_TOOL_OUTPUT_CHARS + 500)
    sent, cut = agent.truncate_for_model(long_text)
    assert cut and "[truncated: showing" in sent and str(len(long_text)) in sent
    assert sent.startswith("y" * agent.MAX_TOOL_OUTPUT_CHARS)


def test_redaction_and_log_never_has_key():
    assert agent.redact("a " + FAKE_KEY + " b") == "a [REDACTED] b"
    assert "sk-ant-" not in agent.redact("other sk-ant-api03-ZZZ-yy token")
    with tempfile.TemporaryDirectory() as tmp:
        log = make_log(tmp)
        log.write("x", note="leak " + FAKE_KEY)
        assert FAKE_KEY not in log.path.read_text()
    _, printed = quiet(agent.say, "key is " + FAKE_KEY)
    assert FAKE_KEY not in printed


def test_cost():
    cost = agent.estimate_cost(1000000, 1000000)
    assert abs(cost - (agent.PRICE_INPUT_PER_MTOK + agent.PRICE_OUTPUT_PER_MTOK)) < 1e-12
    assert agent.estimate_cost(0, 0) == 0


def test_approval_only_exact_y():
    real_run = agent.subprocess.run
    try:
        agent.subprocess.run = must_not_run
        with tempfile.TemporaryDirectory() as tmp:
            log = make_log(tmp)
            for answer in ["n", "", "yes", "Y", "no", "  "]:
                (out, err), _ = quiet(agent.execute_tool, "run_profile", {}, log, lambda p, a=answer: a)
                assert out == "denied by user" and err, answer

            def eof(prompt):
                raise EOFError
            (out, err), _ = quiet(agent.execute_tool, "run_profile", {}, log, eof)
            assert out == "denied by user"
            ev = [e for e in log_events(log) if e["event"] == "tool_call"]
            assert len(ev) == 7 and all(e["approved"] is False for e in ev)
    finally:
        agent.subprocess.run = real_run

    # 'y' (with spaces around) approves a harmless read-only tool
    with tempfile.TemporaryDirectory() as tmp:
        agent.RESULTS_DIR = Path(tmp)
        (Path(tmp) / "a.csv").write_text("h\n")
        log = make_log(tmp)
        (out, err), _ = quiet(agent.execute_tool, "list_results", {}, log, lambda p: " y ")
        assert out == "a.csv" and not err
        ev = log_events(log)[-1]
        assert ev["approved"] is True and ev["output"] == "a.csv"


def test_benchmark_name_whitelist():
    real_run = agent.subprocess.run
    try:
        agent.subprocess.run = must_not_run
        for bad in ["run-everything", "run-sweep; rm -rf /", "", None]:
            out, err = agent.tool_run_benchmark({"name": bad})
            assert err and out.startswith("rejected")
    finally:
        agent.subprocess.run = real_run


def test_loop_tool_then_answer():
    with tempfile.TemporaryDirectory() as tmp:
        agent.RESULTS_DIR = Path(tmp)
        (Path(tmp) / "m.csv").write_text("h\n")
        log = make_log(tmp)
        client = FakeClient([reply_tool("list_results", {}), reply_text("done")])
        code, printed = quiet(agent.run_agent, client, "m", "q", 10, 100000, log, lambda p: "y")
        assert code == 0 and client.calls == 2 and "done" in printed
        # second call must carry exactly one user message with the tool result
        last = client.seen[1][-1]
        assert last["role"] == "user" and last["content"][0]["type"] == "tool_result"
        assert last["content"][0]["content"] == "m.csv"
        kinds = [e["event"] for e in log_events(log)]
        assert kinds.count("api_call") == 2 and kinds.count("tool_call") == 1 and "final" in kinds


def test_loop_denied_tool():
    real_run = agent.subprocess.run
    try:
        agent.subprocess.run = must_not_run
        with tempfile.TemporaryDirectory() as tmp:
            log = make_log(tmp)
            client = FakeClient([reply_tool("run_profile", {}), reply_text("ok")])
            code, _ = quiet(agent.run_agent, client, "m", "q", 10, 100000, log, lambda p: "n")
            assert code == 0
            result = client.seen[1][-1]["content"][0]
            assert result["content"] == "denied by user" and result["is_error"] is True
    finally:
        agent.subprocess.run = real_run


def test_limits():
    with tempfile.TemporaryDirectory() as tmp:
        agent.RESULTS_DIR = Path(tmp)
        log = make_log(tmp)
        # max steps: the model keeps asking for tools
        client = FakeClient([reply_tool("list_results", {}) for _ in range(5)])
        code, printed = quiet(agent.run_agent, client, "m", "q", 2, 100000, log, lambda p: "y")
        assert code == 2 and client.calls == 2 and "maximum of 2 steps" in printed
        # token budget: stops BEFORE running the requested tool
        log2 = make_log(tmp + "/b")
        client = FakeClient([reply_tool("list_results", {}, i=900, o=200)])
        code, printed = quiet(agent.run_agent, client, "m", "q", 10, 1000, log2, lambda p: "y")
        assert code == 2 and "token budget reached" in printed
        assert not [e for e in log_events(log2) if e["event"] == "tool_call"]


def test_api_errors_stop_cleanly():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    cases = [
        (anthropic.RateLimitError("slow down", response=httpx.Response(429, request=req), body=None), "Rate limit"),
        (anthropic.BadRequestError("Your credit balance is too low", response=httpx.Response(400, request=req), body=None), "Billing/credit"),
        (anthropic.AuthenticationError("bad key " + FAKE_KEY, response=httpx.Response(401, request=req), body=None), "API key problem"),
        (anthropic.APIConnectionError(request=req), "Network error"),
        (anthropic.APITimeoutError(request=req), "timed out"),
        (anthropic.InternalServerError("oops", response=httpx.Response(500, request=req), body=None), "HTTP 500"),
    ]
    for exc, expected in cases:
        with tempfile.TemporaryDirectory() as tmp:
            log = make_log(tmp)
            client = FakeClient([exc])
            code, printed = quiet(agent.run_agent, client, "m", "q", 10, 100000, log, lambda p: "y")
            assert code == 1 and expected in printed, (type(exc).__name__, printed)
            assert FAKE_KEY not in printed and FAKE_KEY not in log.path.read_text()
            assert any(e["event"] == "api_error" for e in log_events(log))


def test_non_normal_stop_reason():
    with tempfile.TemporaryDirectory() as tmp:
        log = make_log(tmp)
        resp = reply_text("cut off")
        resp.stop_reason = "max_tokens"
        code, printed = quiet(agent.run_agent, FakeClient([resp]), "m", "q", 10, 100000, log, lambda p: "y")
        assert code == 2 and "stop_reason=max_tokens" in printed


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("ok  ", t.__name__)
    print("%d tests passed" % len(tests))
