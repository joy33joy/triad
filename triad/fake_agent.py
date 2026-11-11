"""Offline stand-in for the three vendor CLIs (used by `triad --demo` and the tests).

It speaks each vendor's real streaming JSON format, so the demo exercises the
same parsers, chat board and orchestration as a live run, using no quota.
Knobs (environment variables) used by the tests:
  TRIAD_FAKE_QUOTA=codex       agents that hit their usage limit when building
  TRIAD_FAKE_WORK=3            seconds each engineer "works" after checking in
  TRIAD_FAKE_HALT=codex        the lead's direction check halts this engineer
  TRIAD_FAKE_REPLAN=1          the lead's direction check halts everyone for a new plan (once)
  TRIAD_FAKE_APPROVE=1         the lead approves on the first review
  TRIAD_FAKE_STAFF=1           the lead's plan hires a second codex (codex-2) to write docs/USAGE.md
  TRIAD_FAKE_BENCH=antigravity the lead benches this member at the first stage review
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

AGENT = os.environ.get("TRIAD_AGENT", "claude")              # who I am on the team (claude, codex-2, ...)
FORMAT = os.environ.get("TRIAD_PROVIDER", re.sub(r"-\d+$", "", AGENT))  # which vendor CLI I imitate
DELAY = float(os.environ.get("TRIAD_FAKE_DELAY", "0.4"))
CWD = Path.cwd()

FILES = {
    "wordcount/core.py": '''"""Counting logic."""


def count(text: str) -> dict:
    """Return word, line and character counts for text."""
    return {
        "words": len(text.split()),
        "lines": text.count("\\n") + (1 if text and not text.endswith("\\n") else 0),
        "chars": len(text),
    }
''',
    "wordcount/__init__.py": "from .core import count\n\n__all__ = [\"count\"]\n",
    "wordcount/cli.py": '''"""Command line entry point: python3 -m wordcount.cli [FILE]."""
import argparse
import sys

from .core import count


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Count words, lines and characters.")
    parser.add_argument("file", nargs="?", help="file to read (default: stdin)")
    args = parser.parse_args(argv)
    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            text = handle.read()
    else:
        text = sys.stdin.read()
    result = count(text)
    print(f"{result['lines']} lines  {result['words']} words  {result['chars']} chars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
''',
    "tests/test_wordcount.py": '''import io
import unittest
from contextlib import redirect_stdout

from wordcount import count
from wordcount.cli import main


class WordCountTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(count(""), {"words": 0, "lines": 0, "chars": 0})

    def test_basic(self):
        self.assertEqual(count("hello world\\nbye\\n"), {"words": 3, "lines": 2, "chars": 16})

    def test_no_trailing_newline(self):
        self.assertEqual(count("a b")["lines"], 1)

    def test_cli(self, ):
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt") as f:
            f.write("one two three\\n")
        out = io.StringIO()
        with redirect_stdout(out):
            main([f.name])
        os.unlink(f.name)
        self.assertIn("3 words", out.getvalue())


if __name__ == "__main__":
    unittest.main()
''',
}

BUILD_STAGE = {
    "name": "Build", "type": "build", "goal": "A working word counter with tests.",
    "assignments": [
        {"agent": "claude", "role": "Core library engineer", "task": "Implement wordcount.core.count and the package __init__.",
         "owns": ["wordcount/core.py", "wordcount/__init__.py"], "deliverables": ["count() per the contract"]},
        {"agent": "codex", "role": "CLI engineer", "task": "Implement the command line tool in wordcount/cli.py.",
         "owns": ["wordcount/cli.py"], "deliverables": ["python3 -m wordcount.cli FILE works"]},
        {"agent": "antigravity", "role": "QA engineer", "task": "Write a unittest suite covering the library and CLI, including edge cases.",
         "owns": ["tests/"], "deliverables": ["tests/test_wordcount.py"]},
    ],
}
PLAN = {
    "client_brief": "You'll get **wordcount**, a small tool that counts words, lines and characters, with tests "
                    "and a README.\n\n1. 🔎 Quick research on how `wc` counts\n2. ⚖ A short design debate\n"
                    "3. 🎲 A role-play of real users trying it\n4. 🛠 Build and test\n\nAbout four short stages.",
    "questions": ["Should a last line without a newline count as a line?"],
    "project_name": "wordcount",
    "summary": "A tiny, dependency-free word counter, designed with a bit of research, a debate and a usage simulation.",
    "deliverable": "The wordcount package, its tests, README.md and DELIVERY.md.",
    "architecture": "Python 3 package `wordcount/`. Contract: `wordcount.core.count(text: str) -> dict` with keys "
                    "words, lines, chars. CLI: `python3 -m wordcount.cli [FILE]` prints '<l> lines  <w> words  <c> chars'. "
                    "Tests use unittest in tests/. Notes go in work/.",
    "stages": [
        {"name": "Background research", "type": "research", "goal": "How do wc and friends count words and lines?",
         "assignments": [{"agent": a, "role": "Researcher", "task": f"Research topic {i + 1} about counting text."}
                         for i, a in enumerate(["claude", "codex", "antigravity"])]},
        {"name": "Design debate", "type": "debate", "topic": "Should an unterminated last line count as a line?",
         "rounds": 3, "positions": {"claude": "Yes, count it", "codex": "No, match wc exactly", "antigravity": "Make it configurable"}},
        {"name": "Usage simulation", "type": "simulate", "goal": "Stress-test the CLI with realistic users.", "turns": 2,
         "actors": {"claude": "a data engineer piping logs", "codex": "a student counting an essay"},
         "scenarios": [{"name": "Big log file", "setup": "A 2 GB log file is piped into the tool on a laptop."}]},
        BUILD_STAGE,
    ],
    "verify": "python3 -m unittest discover -s tests -q",
}
if os.environ.get("TRIAD_FAKE_PLAN") == "software":
    PLAN = {**PLAN, "stages": [BUILD_STAGE]}
if os.environ.get("TRIAD_FAKE_STAFF"):
    PLAN = {**PLAN, "staffing": {"add": [{"provider": "codex", "reason": "usage docs can be written in parallel"}], "bench": []},
            "stages": [st if st["type"] != "build" else {**st, "assignments": st["assignments"] + [
                {"agent": "codex-2", "role": "Docs writer", "task": "Write docs/USAGE.md with examples.",
                 "owns": ["docs/USAGE.md"]}]} for st in PLAN["stages"]]}


def emit(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


def tool(name: str, params: dict) -> None:
    if FORMAT == "claude":
        emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": params}]}})
    elif FORMAT == "codex":
        if "command" in params:
            emit({"type": "item.started", "item": {"type": "command_execution", "command": f"/bin/bash -lc '{params['command']}'"}})
        else:
            emit({"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": params.get("file_path", ""), "kind": "add"}]}})
    else:
        p = {"CommandLine": params["command"]} if "command" in params else {"Path": params.get("file_path", "")}
        emit({"event": "step_update", "step_update": {"step_type": "tool", "state": "ACTIVE",
                                                      "tool_name": "run_command" if "command" in params else "write_file",
                                                      "tool_info": {"parameters": p}}})
    time.sleep(DELAY)


def say(text: str) -> None:
    if FORMAT == "claude":
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    elif FORMAT == "codex":
        emit({"type": "item.completed", "item": {"type": "agent_message", "text": text}})


def finish(text: str, ok: bool = True, error: str = "") -> None:
    usage = {"input_tokens": 1200 + len(text), "output_tokens": 300 + len(text) // 4}
    if FORMAT == "claude":
        if ok:
            emit({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
                "five_hour": {"utilization": 0.12}, "seven_day": {"utilization": 0.31}}}})
        emit({"type": "result", "subtype": "success" if ok else "error", "is_error": not ok, "result": text or error, "usage": usage})
    elif FORMAT == "codex":
        if ok:
            emit({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
            emit({"type": "turn.completed", "usage": usage})
        else:
            emit({"type": "turn.failed", "error": {"message": error}})
    else:
        emit({"event": "result", "result": {"status": "SUCCESS" if ok else "ERROR", "response": text,
                                            "error": error, "usage": usage}})
    sys.exit(0 if ok else 1)


def chat(*args: str) -> str:
    cmd = [sys.executable, ".triad/msg.py", "--as", AGENT, *args]
    tool("Bash", {"command": " ".join(cmd[1:3] + [repr(a) if " " in a else a for a in cmd[3:]])})
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def read_prompt(argv: list[str]) -> str:
    if "-p" in argv and argv.index("-p") + 1 < len(argv) and FORMAT == "antigravity":
        return argv[argv.index("-p") + 1]
    return sys.stdin.read()


def main() -> None:
    prompt = read_prompt(sys.argv[1:])
    role = re.search(r"TRIAD_ROLE:\s*(\w+)", prompt)
    role = role.group(1) if role else "chat"
    if FORMAT in os.environ.get("TRIAD_FAKE_QUOTA", "").split(",") and role in ("work", "sync"):
        time.sleep(DELAY)
        finish("", ok=False, error="429 rate_limit_exceeded: You've hit your usage limit. Try again in 2 hours.")

    if role == "brainstorm":
        tool("Glob", {"pattern": "**/*"})
        idea = {"claude": "Keep the core a pure function with a precise contract so CLI and tests can be built in parallel.",
                "codex": "Ship a zero-dependency CLI via `python -m`; read stdin when no file is given, like wc.",
                "antigravity": "Edge cases: empty input, no trailing newline, unicode. I'd like to own QA."}[FORMAT]
        finish(f"Proposal from {AGENT}: {idea}")
    if role == "plan":
        finish(json.dumps(PLAN))
    stage_type = (re.search(r"STAGE TYPE: (\w+)", prompt) or [None, "build"])[1]
    in_build_stage = 'stage "Build"' in prompt or "STAGE 4: Build" in prompt or "CURRENT STAGE 1: Build" in prompt
    if role == "work" and stage_type == "research":
        owns = re.search(r"You own \(write freely\): (.+)", prompt).group(1).split(", ")[0]
        chat("checkin", f"I'll write my notes to {owns}.")
        tool("WebSearch", {"query": "how does wc count lines"})
        target = CWD / owns
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# Notes by {AGENT}\n\n- POSIX wc counts newline characters, so an unterminated last line "
                          "is not counted (source: https://pubs.opengroup.org/onlinepubs/9699919799/utilities/wc.html).\n")
        tool("Write", {"file_path": str(target)})
        time.sleep(float(os.environ.get("TRIAD_FAKE_WORK", "0")))
        finish(f"STATUS: READY\n## What I produced (files)\n{owns}\n## Key findings / interfaces others rely on\n"
               "wc counts newlines.\n## How to use / verify it\nRead it.\n## Open issues and uncertainties\nNone")
    if role == "work":
        owns = re.search(r"You own \(write freely\): (.+)", prompt)
        owned = owns.group(1) if owns else ""
        redirected = "THE LEAD STOPPED YOU" in prompt
        chat("inbox")
        if redirected:
            say("Correcting course as the lead asked.")
        else:
            chat("checkin", f"I'll implement {owned} per the contract, stdlib only.")
        if AGENT == "codex" and not redirected:
            answer = chat("ask", "Should the CLI read stdin when no FILE is given?")
            say(f"Lead says: {answer.strip()[:120]}")
        time.sleep(float(os.environ.get("TRIAD_FAKE_WORK", "0")))
        wrote = []
        for path, content in FILES.items():
            if any(path.startswith(o.rstrip("*/")) for o in owned.split(", ") if o):
                target = CWD / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
                tool("Write", {"file_path": str(target)})
                wrote.append(path)
        for o in owned.split(", "):
            if o and o.endswith(".md") and not wrote:
                target = CWD / o
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"# Usage\n\nWritten by {AGENT}.\n\n    python3 -m wordcount.cli FILE\n")
                tool("Write", {"file_path": str(target)})
                wrote.append(o)
        say(f"Wrote {', '.join(wrote) or 'nothing'}.")
        if "wordcount/core.py" in wrote:
            chat("send", "all", "core is in: count(text) -> {words, lines, chars}. Lines count a final unterminated line.")
        elif "wordcount/cli.py" in wrote:
            chat("send", "claude", "Does count() treat a missing trailing newline as a line? CLI output depends on it.")
        else:
            chat("send", "all", "Tests are in tests/test_wordcount.py; run python3 -m unittest discover -s tests")
        finish(f"STATUS: CHANGED\n## What I produced (files)\n{', '.join(wrote)}\n## Key findings / interfaces others rely on\n"
               "Per plan contract.\n## How to use / verify it\npython3 -m unittest discover -s tests -q\n## Open issues and uncertainties\nNone")
    if role == "sync":
        inbox = chat("inbox")
        if AGENT == "claude" and "trailing newline" in inbox:
            chat("send", "codex", "Yes: 'a b' (no newline) counts as 1 line.")
        tool("Bash", {"command": "python3 -m unittest discover -s tests -q"})
        finish("STATUS: READY\n## What I produced (files)\nNo changes needed; my part integrates cleanly.\n"
               "## Key findings / interfaces others rely on\nUnchanged.\n## How to use / verify it\nSee README.\n## Open issues and uncertainties\nNone")
    if role == "debate":
        rnd = int(re.search(r"round (\d+) of (\d+)", prompt).group(1))
        position = re.search(r"YOUR ASSIGNED POSITION: (.+)", prompt).group(1)
        tool("Read", {"file_path": str(CWD / "work/01-background-research/SUMMARY.md")})
        if rnd == 1:
            finish(f"I argue: {position}. POSIX wc counts newline characters, but users think in lines. 70% of users expect "
                   "the last line to count.\nPOSITION: HOLD")
        finish("Counting the unterminated line matches what users expect, and we can document the difference from wc. "
               "I now agree with counting it.\nPOSITION: CONVERGED")
    if role == "simulate_move":
        actor = re.search(r"YOU PLAY: (.+)", prompt).group(1)
        finish(f"PRIVATE REASONING: As {actor} I want results fast without running out of memory.\n"
               f"MOVE: {actor} runs `cat big.log | python3 -m wordcount.cli` and watches memory use.")
    if role == "simulate_gm":
        turn, turns = map(int, re.search(r"Turn (\d+) of (\d+)", prompt).groups())
        last = turn >= turns
        finish(json.dumps({"narrative": f"Turn {turn}: the tool reads the whole input into memory; on the 2 GB file the "
                                        "laptop starts swapping, the data engineer kills it after a minute.",
                           "state": "Users learned large inputs need streaming.", "ended": last,
                           "outcome": "Streaming input is needed for big files." if last else ""}))
    if role == "direction":
        tool("Read", {"file_path": str(CWD / ".triad/plan.json")})
        halt = os.environ.get("TRIAD_FAKE_HALT", "")
        if os.environ.get("TRIAD_FAKE_REPLAN") and "Replans used: 0" in prompt and 'stage "Build"' in prompt:
            finish(json.dumps({"verdict": "halt", "notes": {}, "halt": [],
                               "halt_all": "The CLI must be a package entry point; restructure assignments."}))
        if halt and 'stage "Build"' in prompt:
            finish(json.dumps({"verdict": "halt", "notes": {},
                               "halt": [{"agent": halt, "reason": "Stick to the contract: stdlib only, no click."}], "halt_all": ""}))
        finish(json.dumps({"verdict": "on_track", "notes": {}, "halt": [], "halt_all": ""}))
    if role == "answer":
        askers = sorted(set(re.findall(r"^- from (\w+):", prompt, re.M)))
        finish(json.dumps({"answers": [{"to": a, "text": "Yes: read stdin when no FILE is given, like wc."} for a in askers],
                           "halt": [], "halt_all": ""}))
    if role == "narrate":
        stage = re.findall(r"=== (Stage [^\n]+)", prompt)
        finish(f"The team is on {stage[-1] if stage else 'planning'}; work is moving steadily and nothing is blocked.")
    if role == "client_desk":
        message = re.search(r"THE CLIENT'S MESSAGE:\n<<<\n(.*?)\n>>>", prompt, re.S).group(1)
        if "pivot" in message.lower():
            finish(json.dumps({"reply": "Understood: I'm stopping the team and reworking the plan for you to approve.",
                               "requirement": "Ship a JSON output mode.", "notes": {}, "halt": [], "next_stages": None,
                               "replan": "The client wants JSON output; the current work doesn't cover it."}))
        if "verbose" in message.lower():
            finish(json.dumps({"reply": "Good idea: I've asked Codex to add a --verbose flag.",
                               "requirement": "The CLI must support --verbose.", "notes": {"codex": "Add a --verbose flag."},
                               "halt": [], "next_stages": None, "replan": ""}))
        finish(json.dumps({"reply": "We're on track: research is done and the build is underway.", "requirement": "",
                           "notes": {}, "halt": [], "next_stages": None, "replan": ""}))
    if role == "gate":
        m = re.search(r'Stage (\d+) "([^"]+)" \((\w+)\) just finished', prompt)
        kind = m.group(3)
        first = "Review round 1 of" in prompt and not os.environ.get("TRIAD_FAKE_APPROVE")
        next_stages = None
        if kind == "research" and os.environ.get("TRIAD_FAKE_ADAPT"):
            next_stages = [st for st in PLAN["stages"][1:] if st["type"] != "simulate"]  # skip the simulation
        summary = {"research": "wc counts newline characters; an unterminated last line is not counted.",
                   "debate": "The team converged: count the unterminated last line and document the difference from wc.",
                   "simulate": "Large inputs exhaust memory; the CLI should stream.",
                   "build": "Core, CLI and tests agree on the contract."}[kind]
        if kind == "build" and first:
            finish(json.dumps({"approved": False, "summary": "Parts fit; tests miss unicode input.",
                               "feedback": {"antigravity": "Add a test that counts unicode words like 'héllo wörld'."},
                               "next_stages": None, "halt_all": ""}))
        tool("Read", {"file_path": str(CWD / "work")})
        staffing = {"add": [], "bench": [os.environ["TRIAD_FAKE_BENCH"]]} if os.environ.get("TRIAD_FAKE_BENCH") and m.group(1) == "1" else {}
        finish(json.dumps({"approved": True, "summary": summary, "feedback": {}, "next_stages": next_stages, "halt_all": "",
                           "staffing": staffing}))
    if role == "fix":
        target = CWD / "tests/test_unicode.py"
        target.write_text("import unittest\nfrom wordcount import count\n\n\nclass Unicode(unittest.TestCase):\n"
                          "    def test_unicode(self):\n        self.assertEqual(count('héllo wörld')['words'], 2)\n")
        tool("Write", {"file_path": str(target)})
        tool("Bash", {"command": "python3 -m unittest discover -s tests -q"})
        finish("STATUS: CHANGED\n## What I produced (files)\nAdded tests/test_unicode.py as the lead asked.\n"
               "## Key findings / interfaces others rely on\nUnchanged.\n## How to use / verify it\npython3 -m unittest discover -s tests\n## Open issues and uncertainties\nNone")
    if role == "integrate":
        tool("Bash", {"command": "python3 -m unittest discover -s tests -q"})
        (CWD / "README.md").write_text("# wordcount\n\nCount words, lines and characters.\n\n"
                                      "```\npython3 -m wordcount.cli FILE\npython3 -m unittest discover -s tests\n```\n")
        tool("Write", {"file_path": str(CWD / "README.md")})
        (CWD / "DELIVERY.md").write_text("# Delivery: wordcount\n\n- Core library: Claude\n- CLI: Codex\n- Tests: Antigravity\n")
        tool("Write", {"file_path": str(CWD / "DELIVERY.md")})
        finish("Delivered `wordcount`: a library, a CLI (`python3 -m wordcount.cli FILE`) and a passing test suite. "
               "See README.md and DELIVERY.md.")
    finish("ok")


if __name__ == "__main__":
    main()
