import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from triad.agents import StreamParser, Usage, build_command, classify_error
from triad.bus import Board
from triad.company import Company, parse_plan
from triad.config import Config

TEAM = ["claude", "codex", "antigravity"]


# ------------------------------------------------------------------ parsers (event shapes captured from the real CLIs)

def test_claude_stream(tmp_path):
    p = StreamParser("claude", tmp_path)
    acts = []
    for e in [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "echo hi > note.txt"}}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "input": {"file_path": str(tmp_path / "hello.py")}}]}},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 0.06}, "seven_day": {"utilization": 0.05}}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "DONE"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "DONE", "usage": {"input_tokens": 2, "output_tokens": 4}},
    ]:
        acts += p.feed(json.dumps(e))
    assert [a.text for a in acts] == ["$ echo hi > note.txt", "write hello.py", "DONE"]
    assert p.success and p.final_text == "DONE" and p.quota == {"5h": 0.06, "7d": 0.05}


def test_claude_rate_limited(tmp_path):
    p = StreamParser("claude", tmp_path)
    p.feed(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1893456000}}))
    assert p.rate_limited and p.retry_at == 1893456000


def test_codex_stream(tmp_path):
    p = StreamParser("codex", tmp_path)
    acts = []
    for e in [
        {"type": "thread.started", "thread_id": "x"},
        {"type": "item.started", "item": {"type": "command_execution", "command": "/usr/bin/bash -lc 'python3 -c \"print(42)\" > note.txt'"}},
        {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": str(tmp_path / "hello.py"), "kind": "add"}]}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "DONE"}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 5}},
    ]:
        acts += p.feed(json.dumps(e))
    assert acts[0].text == '$ python3 -c "print(42)" > note.txt'
    assert acts[1].text == "add hello.py"
    assert p.success and p.final_text == "DONE" and p.input_tokens == 40  # cached input not counted


def test_antigravity_stream(tmp_path):
    p = StreamParser("antigravity", tmp_path)
    acts = []
    for e in [
        {"event": "init", "conversation_id": "c"},
        {"event": "step_update", "step_update": {"state": "ACTIVE", "step_type": "tool", "tool_name": "run_command",
                                                 "tool_info": {"name": "run_command", "parameters": {"CommandLine": "echo hi"}}}},
        {"event": "step_update", "step_update": {"state": "DONE", "step_type": "tool", "tool_name": "run_command"}},
        {"event": "result", "result": {"status": "SUCCESS", "response": "DONE\n", "usage": {"input_tokens": 9, "output_tokens": 3}}},
    ]:
        acts += p.feed(json.dumps(e))
    assert [a.text for a in acts] == ["$ echo hi"]
    assert p.success and p.final_text.strip() == "DONE"


def test_garbage_lines_are_ignored(tmp_path):
    assert StreamParser("codex", tmp_path).feed("warning: something\n") == []


def test_classify():
    assert classify_error("429 Too Many Requests") == "quota"
    assert classify_error("You've hit your usage limit") == "quota"
    assert classify_error("Please log in again") == "auth"
    assert classify_error("segfault") == "error"


def test_commands_are_readonly_when_thinking(tmp_path):
    argv, stdin = build_command("codex", "think", tmp_path, "hi")
    assert argv[argv.index("-s") + 1] == "read-only" and stdin == "hi"
    argv, _ = build_command("codex", "work", tmp_path, "hi")
    assert argv[argv.index("-s") + 1] == "workspace-write"
    argv, _ = build_command("antigravity", "think", tmp_path, "hi")
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert "command(*)" not in settings["permissions"]["allow"] and "command(ls)" in settings["permissions"]["allow"]
    assert argv[-2:] == ["-p", "hi"]
    argv, _ = build_command("claude", "think", tmp_path, "hi")
    assert "Bash" not in argv[argv.index("--allowedTools") + 1]


# ------------------------------------------------------------------ plan normalisation

def test_parse_plan_legacy_assignments_become_one_build_stage():
    text = "Here you go:\n```json\n" + json.dumps({
        "project_name": "x", "summary": "s", "architecture": {"a": 1},
        "assignments": [
            {"agent": "Gemini", "task": "ui", "owns": "web/"},
            {"agent": "claude", "task": "api"},
            {"agent": "someone", "task": "cli"},
            {"agent": "claude", "task": "docs"},
        ]}) + "\n```"
    plan = parse_plan(text, TEAM)
    assert [s["type"] for s in plan["stages"]] == ["build"]
    assignments = plan["stages"][0]["assignments"]
    assert [a["agent"] for a in assignments] == TEAM
    by = {a["agent"]: a for a in assignments}
    assert by["antigravity"]["owns"] == ["web/"]
    assert by["codex"]["task"] == "cli"
    assert "docs" in json.dumps(plan)  # the extra assignment was folded in, not dropped
    assert isinstance(plan["architecture"], str)


def test_parse_plan_stages_are_normalised():
    plan = parse_plan(json.dumps({"project_name": "p", "stages": [
        {"name": "Collect", "type": "data collection", "assignments": [{"agent": a, "task": "t"} for a in TEAM]},
        {"name": "Argue", "type": "discussion", "rounds": 9, "positions": {"Codex": "bear"}},
        {"name": "Wargame", "type": "wargame", "turns": 0, "actors": {"claude": "US Senate", "agy": "China"},
         "scenarios": ["Shutdown fight", {"name": "Tariffs", "setup": "New tariffs announced"}, "x", "y"]},
        {"name": "Broken sim", "type": "simulate", "actors": {}},
        {"name": "Mystery", "type": "telepathy", "assignments": [{"agent": "codex", "task": "t"}]},
    ]}), TEAM, lead="claude")
    types = [s["type"] for s in plan["stages"]]
    assert types == ["research", "debate", "simulate", "build"]  # bad sim dropped, unknown type -> build
    research, debate, sim, _ = plan["stages"]
    assert research["assignments"][0]["owns"] == ["work/01-collect/claude.md"]  # default notes location
    assert debate["rounds"] == 3 and debate["positions"]["codex"] == "bear" and set(debate["positions"]) == set(TEAM)
    assert sim["turns"] == 1 and len(sim["scenarios"]) == 3 and sim["game_master"] == "claude"
    assert sim["actors"] == {"claude": "US Senate", "antigravity": "China"}


def test_parse_plan_rejects_nonsense():
    assert parse_plan("I think we should build it.", TEAM) is None
    assert parse_plan('{"assignments": []}', TEAM) is None
    assert parse_plan('{"stages": [{"type": "simulate"}]}', TEAM) is None


def test_parse_plan_smaller_team():
    plan = parse_plan(json.dumps({"assignments": [{"agent": "codex", "task": "a"}, {"agent": "claude", "task": "b"},
                                                  {"agent": "antigravity", "task": "c"}]}), ["claude", "codex"])
    assignments = plan["stages"][0]["assignments"]
    assert [a["agent"] for a in assignments] == ["claude", "codex"]
    assert "c" in assignments[0]["task"] + assignments[1]["task"]


def test_skills_are_loaded_and_user_skills_override(tmp_path, monkeypatch):
    from triad.skills_loader import load_skills
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "triad" / "skills").mkdir(parents=True)
    (tmp_path / "triad" / "skills" / "forecasting.md").write_text("My own forecasting rules.\n- be bold")
    (tmp_path / "triad" / "skills" / "legal.md").write_text("Legal review checklist.")
    skills = load_skills(Config())
    assert {"website", "report-writing", "data-collection", "legal"} <= set(skills)
    assert skills["forecasting"].startswith("My own forecasting rules")


# ------------------------------------------------------------------ team chat

def test_msg_tool_roundtrip(tmp_path):
    board = Board(tmp_path, TEAM, lead="claude")
    board.install()
    tool = [sys.executable, str(tmp_path / ".triad/msg.py")]
    run = lambda *a: subprocess.run(tool + list(a), cwd=tmp_path, capture_output=True, text=True)
    assert "sent #1" in run("--as", "codex", "send", "claude", "what is the port?").stdout
    assert run("--as", "codex", "send", "bob", "hi").returncode != 0
    assert "what is the port?" in run("--as", "claude", "inbox").stdout
    assert "no new messages" in run("--as", "claude", "inbox").stdout  # cursor advanced
    assert "no new messages" in run("--as", "antigravity", "inbox").stdout  # not addressed to them
    run("--as", "antigravity", "checkin", "I will write tests/ first")
    assert "no new messages" in run("--as", "claude", "inbox").stdout  # the lead's mailbox is separate from claude's
    board.post("user", "all", "make it blue")
    assert "make it blue" in run("--as", "antigravity", "wait", "--timeout", "1").stdout
    msgs = board.poll()
    assert [m["text"] for m in msgs] == ["what is the port?", "I will write tests/ first", "make it blue"]
    assert msgs[1]["to"] == "lead" and msgs[1]["kind"] == "checkin"
    # ask blocks until the lead replies
    import threading, time as _t
    threading.Timer(0.6, lambda: board.post("lead", "codex", "Use port 8080.", kind="answer")).start()
    t0 = _t.time()
    out = run("--as", "codex", "ask", "which port?", "--timeout", "10").stdout
    assert "Use port 8080." in out and _t.time() - t0 < 5
    assert "not answered" in run("--as", "codex", "ask", "anyone?", "--timeout", "0.5").stdout
    assert [m["text"] for m in board.poll()] == ["which port?", "Use port 8080.", "anyone?"]
    assert board.poll() == []


# ------------------------------------------------------------------ full company runs with the offline agents

def fast_config(**overrides) -> Config:
    cfg = Config()
    cfg.poll_seconds, cfg.question_batch_seconds, cfg.checkin_timeout = 0.05, 0.2, 20
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _run_company(workspace: Path, monkeypatch, quota_out: str = "", env: dict | None = None,
                 cfg: Config | None = None) -> tuple[Company, list]:
    monkeypatch.setenv("TRIAD_FAKE_DELAY", "0")
    if quota_out:
        monkeypatch.setenv("TRIAD_FAKE_QUOTA", quota_out)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    events = []
    plans = []

    async def approve(plan):
        plans.append(plan)
        return "approve", ""

    company = Company(workspace, cfg or fast_config(), events.append, approve, demo=True)
    company.approved_plans = plans
    asyncio.run(company.run("build a word counter"))
    return company, events


def test_demo_project_end_to_end(tmp_path, monkeypatch):
    company, events = _run_company(tmp_path, monkeypatch)
    kinds = [e.kind for e in events]
    phases = [e.text for e in events if e.kind == "phase"]
    stage_phases = [p for p in phases if p.startswith("Stage ")]
    assert stage_phases == ["Stage 1/4 · Research: Background research", "Stage 2/4 · Debate: Design debate",
                            "Stage 3/4 · Simulate: Usage simulation", "Stage 4/4 · Build: Build"]
    assert phases[:2] == ["Brainstorm", "Planning"] and phases[-1] == "Final assembly"
    assert kinds[-1] == "done"
    for f in ("wordcount/core.py", "wordcount/cli.py", "tests/test_wordcount.py", "README.md", "DELIVERY.md",
              "work/01-background-research/codex.md", "work/01-background-research/SUMMARY.md",
              "work/02-design-debate/debate.md", "work/03-usage-simulation/big-log-file.md"):
        assert (tmp_path / f).exists(), f
    chats = [e for e in events if e.kind == "chat"]
    assert {e.agent for e in chats} >= {"claude", "codex", "antigravity"}  # everyone talked
    assert any(e.agent == "claude" and e.data["to"] == "codex" for e in chats)  # and answered a question
    test = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=tmp_path, capture_output=True)
    assert test.returncode == 0
    log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert "triad: stage 1 Background research" in log and "triad: stage 4 fixes 1" in log and "triad: delivery" in log
    tracked = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert ".triad" not in tracked and "__pycache__" not in tracked
    assert all(u.calls >= 3 for u in company.usage.values())  # the load really was spread
    assert [h["type"] for h in company.history] == ["research", "debate", "simulate", "build"]


def test_debate_runs_rounds_and_stops_when_everyone_converges(tmp_path, monkeypatch):
    company, events = _run_company(tmp_path, monkeypatch)
    turns = [e for e in events if e.kind == "turn" and e.data["kind"] == "debate"]
    assert len(turns) == 6  # 3 members x 2 rounds, although 3 rounds were planned
    assert any("converged" in e.text for e in events if e.kind == "log")
    debate = (tmp_path / "work/02-design-debate/debate.md").read_text()
    assert "Opening statements" in debate and "Make it configurable" in debate
    assert "converged" in (tmp_path / "work/02-design-debate/SUMMARY.md").read_text()


def test_simulation_is_role_play_adjudicated_by_a_game_master(tmp_path, monkeypatch):
    company, events = _run_company(tmp_path, monkeypatch)
    moves = [e for e in events if e.kind == "turn" and e.data["kind"] == "move"]
    gms = [e for e in events if e.kind == "turn" and e.data["kind"] == "gm"]
    assert len(moves) == 4 and {m.agent for m in moves} == {"claude", "codex"}  # antigravity had no actor
    assert len(gms) == 2 and "Outcome" in gms[-1].text
    log = (tmp_path / "work/03-usage-simulation/big-log-file.md").read_text()
    assert "## Turn 2" in log and "Game master" in log and "Streaming input is needed" in log


def test_lead_can_adapt_the_remaining_workflow(tmp_path, monkeypatch):
    company, events = _run_company(tmp_path, monkeypatch, env={"TRIAD_FAKE_ADAPT": "1"})
    adapts = [e for e in events if e.kind == "adapt"]
    assert len(adapts) == 1 and [s["type"] for s in adapts[0].data["stages"]] == ["debate", "build"]
    stage_phases = [e.text for e in events if e.kind == "phase" and e.text.startswith("Stage ")]
    assert not any("Simulate" in p for p in stage_phases)
    assert [h["type"] for h in company.history] == ["research", "debate", "build"]


def test_software_only_plan(tmp_path, monkeypatch):
    company, events = _run_company(tmp_path, monkeypatch, env={"TRIAD_FAKE_PLAN": "software"})
    assert [h["type"] for h in company.history] == ["build"] and not (tmp_path / "work/01-build").exists()
    assert events[-1].kind == "done"


def test_lead_review_loop_sends_targeted_fixes(tmp_path, monkeypatch):
    company, events = _run_company(tmp_path, monkeypatch, env={"TRIAD_FAKE_PLAN": "software"})
    reviews = [e for e in events if e.kind == "review" and e.data["kind"] == "review"]
    assert [r.data["approved"] for r in reviews] == [False, True]  # changes requested, then approved
    assert reviews[0].data["feedback"] == {"antigravity": "Add a test that counts unicode words like 'héllo wörld'."}
    fix_phase = [e.text for e in events if e.kind == "phase" and e.text.startswith("Fixes")]
    assert fix_phase == ["Fixes requested by the lead (Antigravity)"]
    # only the member with feedback did a fix turn: claude/codex did no work between the two reviews
    first, second = events.index(reviews[0]), events.index(reviews[1])
    workers = {e.agent for e in events[first:second] if e.kind == "status" and e.text == "working"}
    assert workers == {"antigravity"}
    assert (tmp_path / "tests/test_unicode.py").exists()
    feedback_msgs = [e for e in events if e.kind == "chat" and e.agent == "lead" and e.data.get("kind") == "review"]
    assert feedback_msgs and feedback_msgs[0].data["to"] == "antigravity"


def test_first_review_approval_skips_fix_rounds(tmp_path, monkeypatch):
    company, events = _run_company(tmp_path, monkeypatch, env={"TRIAD_FAKE_APPROVE": "1", "TRIAD_FAKE_PLAN": "software"})
    reviews = [e for e in events if e.kind == "review" and e.data["kind"] == "review"]
    assert len(reviews) == 1 and reviews[0].data["approved"]
    assert not [e for e in events if e.kind == "phase" and e.text.startswith("Fixes")]


def test_questions_are_answered_by_the_lead_and_direction_checked_once(tmp_path, monkeypatch):
    company, events = _run_company(tmp_path, monkeypatch)
    answers = [e for e in events if e.kind == "chat" and e.agent == "lead" and e.data.get("kind") == "answer"]
    assert answers and answers[0].data["to"] == "codex" and "stdin" in answers[0].text
    assert company.lead_answers == 1
    directions = [e for e in events if e.kind == "review" and e.data["kind"] == "direction"]
    assert len(directions) == 1 and directions[0].text == "on_track"
    checkins = [e for e in events if e.kind == "chat" and e.data.get("kind") == "checkin"]
    assert {e.agent for e in checkins} == {"claude", "codex", "antigravity"}


def test_question_budget_falls_back_without_spending_usage(tmp_path, monkeypatch):
    company, events = _run_company(tmp_path, monkeypatch, cfg=fast_config(lead_answer_budget=0))
    answers = [e for e in events if e.kind == "chat" and e.agent == "lead" and e.data.get("kind") == "answer"]
    assert answers and "budget" in answers[0].text and company.lead_answers == 0


def test_lead_halts_one_engineer_who_restarts_with_correction(tmp_path, monkeypatch):
    company, events = _run_company(tmp_path, monkeypatch, env={"TRIAD_FAKE_HALT": "codex", "TRIAD_FAKE_WORK": "1.5"})
    halts = [e for e in events if e.kind == "halt"]
    assert len(halts) == 1 and halts[0].agent == "codex" and halts[0].data["scope"] == "agent"
    after = events[events.index(halts[0]):]
    assert any(e.kind == "activity" and e.agent == "codex" and "Correcting course" in e.text for e in after)
    assert (tmp_path / "wordcount/cli.py").exists() and events[-1].kind == "done"


def test_lead_halts_everyone_for_a_replan_once(tmp_path, monkeypatch):
    company, events = _run_company(tmp_path, monkeypatch, env={"TRIAD_FAKE_REPLAN": "1", "TRIAD_FAKE_WORK": "1.5"})
    halts = [e for e in events if e.kind == "halt"]
    assert [h.data["scope"] for h in halts] == ["all"]
    phases = [e.text for e in events if e.kind == "phase"]
    assert "Replanning" in phases and phases.count("Stage 4/4 · Build: Build") == 2  # the revised plan ran again
    assert company.replans == 1 and len(company.approved_plans) == 2  # the client approved the revised plan too
    assert events[-1].kind == "done" and (tmp_path / "wordcount/cli.py").exists()


def test_quota_failover(tmp_path, monkeypatch):
    company, events = _run_company(tmp_path, monkeypatch, quota_out="codex")
    assert not company.usage["codex"].available()
    assert any("taking over codex's area" in e.text for e in events if e.kind == "log")
    assert (tmp_path / "wordcount/cli.py").exists()  # someone else built Codex's part
    assert events[-1].kind == "done"


def test_existing_clean_repo_gets_a_branch(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "x.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    original = subprocess.run(["git", "branch", "--show-current"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    _run_company(tmp_path, monkeypatch)
    now = subprocess.run(["git", "branch", "--show-current"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    assert now.startswith("triad/wordcount-") and now != original


def test_config_roundtrip(tmp_path):
    cfg = Config()
    cfg.lead = "codex"
    cfg.agents["antigravity"].model = "gemini-x"
    path = cfg.save(tmp_path / "c.json")
    loaded = Config.load(path)
    assert loaded.lead == "codex" and loaded.agents["antigravity"].model == "gemini-x"


# ------------------------------------------------------------------ the app

def test_tui_full_project(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAD_FAKE_DELAY", "0")
    from triad.tui import TriadApp

    async def go():
        cfg = Config()
        cfg.view = "detail"
        app = TriadApp(tmp_path, cfg, demo=True)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press(*"build a word counter", "enter")
            for _ in range(200):
                await pilot.pause(0.05)
                if app.mode == "review":
                    break
            assert app.mode == "review"
            await pilot.press("enter")
            for _ in range(400):
                await pilot.pause(0.05)
                if app.mode == "idle":
                    break
            assert app.mode == "idle"
            chat = "\n".join("".join(s.text for s in line) for line in app.query_one("#chat").lines)
            assert "starts. Lead: Claude" in chat
        return app

    asyncio.run(go())
    assert (tmp_path / "DELIVERY.md").exists()


def test_chat_commands_are_described_plainly():
    from triad.agents import describe_command
    assert describe_command("python3 .triad/msg.py --as codex send claude 'hi there'") == "💬 messages claude"
    assert describe_command("python3 .triad/msg.py --as codex inbox") == "📥 checks team chat"
    assert describe_command("python3 .triad/msg.py --as codex ask 'which port?'").startswith("❓ asks the lead")
    assert describe_command("ls -la") == "$ ls -la"


# ------------------------------------------------------------------ teams of any size and mix

def _run_team(tmp_path, monkeypatch, team, env=None, cfg=None):
    monkeypatch.setenv("TRIAD_FAKE_DELAY", "0")
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    events = []

    async def approve(plan):
        return "approve", ""

    company = Company(tmp_path, cfg or fast_config(), events.append, approve, demo=True, team_spec=team)
    asyncio.run(company.run("build a word counter"))
    return company, events


def test_member_names_and_matching():
    from triad.agents import display, name_members, provider_of
    from triad.company import _canon
    assert [n for n, _, _ in name_members(["claude", "codex", "claude", "gemini:flash"])] == ["claude", "codex", "claude-2", "antigravity"]
    assert name_members(["codex:gpt-6"])[0] == ("codex", "codex", "gpt-6")
    assert display("claude-2") == "Claude #2" and provider_of("codex-3") == "codex"
    team = ["claude", "claude-2", "codex-2"]
    assert _canon("Claude #2", team) == "claude-2" and _canon("claude 2", team) == "claude-2"
    assert _canon("Codex", team) == "codex-2"  # the only codex member
    assert _canon("antigravity", team) == "" and _canon("everyone", team) == "all"


def test_custom_team_without_antigravity(tmp_path, monkeypatch):
    company, events = _run_team(tmp_path, monkeypatch, ["claude", "codex", "claude"], env={"TRIAD_FAKE_PLAN": "software"})
    assert company.team == ["claude", "codex", "claude-2"]
    assert not any(e.agent == "antigravity" for e in events)
    assert company.member_usage["claude-2"].calls >= 1  # the second Claude session really worked
    # both Claude members draw on one subscription
    claude_calls = company.member_usage["claude"].calls + company.member_usage["claude-2"].calls
    assert company.usage["claude"].calls == claude_calls
    assert (tmp_path / "tests/test_wordcount.py").exists()  # claude-2 took the QA assignment
    assert events[-1].kind == "done"


def test_quota_is_per_subscription(tmp_path, monkeypatch):
    company, events = _run_team(tmp_path, monkeypatch, ["claude", "codex", "codex"],
                                env={"TRIAD_FAKE_PLAN": "software", "TRIAD_FAKE_QUOTA": "codex"})
    assert not company.member_available("codex") and not company.member_available("codex-2")
    takeovers = [e.text for e in events if e.kind == "log" and "taking over" in e.text]
    assert takeovers and all("Claude" in t for t in takeovers)  # nobody on the exhausted subscription takes a seat
    assert (tmp_path / "wordcount/cli.py").exists() and events[-1].kind == "done"


def test_lead_hires_a_member_in_the_plan(tmp_path, monkeypatch):
    company, events = _run_team(tmp_path, monkeypatch, None, env={"TRIAD_FAKE_PLAN": "software", "TRIAD_FAKE_STAFF": "1"})
    staff = [e for e in events if e.kind == "staff"]
    assert staff and staff[0].data["added"] == ["codex-2"]
    assert company.team == ["claude", "codex", "antigravity", "codex-2"]
    assert (tmp_path / "docs/USAGE.md").read_text().count("codex-2") == 1
    assert company.member_usage["codex-2"].calls >= 1


def test_hiring_respects_the_cap_and_allowed_providers(tmp_path, monkeypatch):
    company = Company(tmp_path, fast_config(max_team=3), lambda e: None, None, demo=True, team_spec=["claude", "codex"])
    team, hires = company.propose_staffing({"staffing": {"add": [{"provider": "antigravity"}, {"provider": "codex"},
                                                                 {"provider": "claude"}]}}, company.team)
    assert team == ["claude", "codex", "codex-2"]  # antigravity isn't on this team's roster; cap of 3 reached
    assert [h[0] for h in hires] == ["codex-2"]
    team, _ = company.propose_staffing({"staffing": {"bench": ["claude", "codex"]}}, ["claude", "codex"])
    assert team == ["codex"]  # at least one member stays


def test_lead_benches_a_member_and_work_is_redistributed(tmp_path, monkeypatch):
    company, events = _run_team(tmp_path, monkeypatch, None, env={"TRIAD_FAKE_BENCH": "antigravity"})
    staff = [e for e in events if e.kind == "staff"]
    assert staff and staff[0].data["benched"] == ["antigravity"]
    benched_at = events.index(staff[0])
    assert not any(e.kind == "status" and e.agent == "antigravity" and e.text == "working" for e in events[benched_at:])
    build = company.plan["stages"][-1]
    assert [a["agent"] for a in build["assignments"]] == ["claude", "codex"]
    assert "unittest" in json.dumps(build)  # the QA work was folded into someone else's assignment
    assert events[-1].kind == "done"


def test_tui_team_command(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from triad.tui import TriadApp, AgentCard

    async def go():
        app = TriadApp(tmp_path, Config(), demo=True)
        async with app.run_test(size=(140, 40)) as pilot:
            assert [c.agent for c in app.query(AgentCard)] == ["claude", "codex", "antigravity"]
            await pilot.press(*"/team claude,codex,claude,codex", "enter")
            await pilot.pause(0.2)
            assert [c.agent for c in app.query(AgentCard)] == ["claude", "codex", "claude-2", "codex-2"]
        return app

    asyncio.run(go())
    assert Config.load().team == ["claude", "codex", "claude", "codex"]


def test_exhausted_members_are_skipped_not_retried(tmp_path, monkeypatch):
    company, events = _run_team(tmp_path, monkeypatch, None, env={"TRIAD_FAKE_QUOTA": "codex"})
    codex_failures = [e for e in events if e.kind == "error" and e.agent == "codex"]
    assert len(codex_failures) == 1  # discovered once; later debate/simulation seats skip straight to a substitute
    assert any("takes Codex's seat" in e.text for e in events if e.kind == "log") or \
        any("taking over codex's area" in e.text for e in events if e.kind == "log")
    assert events[-1].kind == "done"


# ------------------------------------------------------------------ presenting the plan to the client

def test_plan_brief_and_questions_with_fallback():
    plan = parse_plan(json.dumps({"summary": "A thing.", "deliverable": "thing.zip", "questions": ["Blue or red?", ""],
                                  "stages": [{"name": "Make", "type": "build",
                                              "assignments": [{"agent": a, "task": "t"} for a in TEAM]}]}), TEAM)
    assert plan["questions"] == ["Blue or red?"]
    assert "thing.zip" in plan["client_brief"] and "Make" in plan["client_brief"]  # generated when the lead omits it


def test_client_answers_reach_the_revised_plan_and_the_team(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAD_FAKE_DELAY", "0")
    monkeypatch.setenv("TRIAD_FAKE_PLAN", "software")
    events, decisions = [], [("revise", "1: yes, count it"), ("approve", "")]

    async def answer(plan):
        return decisions.pop(0)

    company = Company(tmp_path, fast_config(), events.append, answer, demo=True)
    asyncio.run(company.run("build a word counter"))
    assert company.plan["client_input"] == ["1: yes, count it"]
    from triad import prompts as P
    assert "1: yes, count it" in P._plan_brief(company.plan)  # every member's prompt carries the answer


def test_lead_thoughts_stay_out_of_the_feed(tmp_path, monkeypatch):
    company, events = _run_company(tmp_path, monkeypatch, env={"TRIAD_FAKE_PLAN": "software"})
    plan_json_lines = [e for e in events if e.kind == "activity" and e.data == "text" and '"stages"' in e.text]
    assert not plan_json_lines


def test_tui_shows_a_brief_and_details_on_request(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAD_FAKE_DELAY", "0")
    from triad.tui import TriadApp

    def text(app):
        return "\n".join("".join(seg.text for seg in line) for line in app.query_one("#summary").lines)

    async def go():
        app = TriadApp(tmp_path, fast_config(), demo=True)
        async with app.run_test(size=(140, 60)) as pilot:
            await pilot.press(*"build a word counter", "enter")
            for _ in range(200):
                await pilot.pause(0.05)
                if app.mode == "review":
                    break
            shown = text(app)
            assert "What I need from you" in shown and "last line without a newline" in shown
            assert "Conventions" not in shown and "shared an approach" in shown
            assert "Proposal from claude" not in shown  # brainstorm text is collapsed
            await pilot.press(*"/details", "enter")
            await pilot.pause(0.2)
            assert "Conventions" in text(app) and "Proposal from claude" in text(app)
            await pilot.press("slash", *"cancel", "enter")
            await pilot.pause(0.5)

    asyncio.run(go())


# ------------------------------------------------------------------ talking to the lead, progress reports, views

def _run_with_client(tmp_path, monkeypatch, messages, env=None, at="Stage 1/"):
    """Run the demo and send the lead `messages` as soon as the given phase starts."""
    monkeypatch.setenv("TRIAD_FAKE_DELAY", "0")
    monkeypatch.setenv("TRIAD_FAKE_WORK", "1.5")
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    events, plans = [], []

    async def approve(plan):
        plans.append(plan)
        return "approve", ""

    company = Company(tmp_path, fast_config(narrator_interval=0.3), lambda e: events.append(e) or sent(e), approve, demo=True)
    pending = list(messages)

    def sent(e):
        if e.kind == "phase" and e.text.startswith(at) and pending:
            company.ask_lead(pending.pop(0))

    asyncio.run(company.run("build a word counter"))
    company.approved_plans = plans
    return company, events


def test_client_asks_the_lead_for_status(tmp_path, monkeypatch):
    company, events = _run_with_client(tmp_path, monkeypatch, ["how is it going?"])
    replies = [e for e in events if e.kind == "lead_reply"]
    assert replies and "on track" in replies[0].text
    assert not [e for e in events if e.kind == "chat" and e.agent == "user"]  # the team never saw it


def test_client_change_reaches_members_and_becomes_a_requirement(tmp_path, monkeypatch):
    company, events = _run_with_client(tmp_path, monkeypatch, ["please add a verbose flag"], at="Stage 4/")
    reply = [e for e in events if e.kind == "lead_reply"][0]
    assert any("Codex" in a for a in reply.data["actions"])
    assert "The CLI must support --verbose." in company.plan["client_input"]
    notes = [e for e in events if e.kind == "chat" and e.agent == "lead" and e.data["to"] == "codex"
             and "verbose" in e.text]
    assert notes


def test_client_pivot_stops_the_team_and_replans_with_approval(tmp_path, monkeypatch):
    company, events = _run_with_client(tmp_path, monkeypatch, ["pivot: we need JSON output"], at="Stage 2/")
    halts = [e for e in events if e.kind == "halt"]
    assert halts and halts[0].data.get("by") == "client"
    phases = [e.text for e in events if e.kind == "phase"]
    assert "Changing direction" in phases
    assert len(company.approved_plans) == 2  # the client approved the revised plan
    assert events[-1].kind == "done"


def test_progress_reports_only_when_something_happened(tmp_path, monkeypatch):
    company, events = _run_with_client(tmp_path, monkeypatch, [])
    reports = [e for e in events if e.kind == "progress"]
    assert reports and all(e.agent == "antigravity" for e in reports)  # prefers Antigravity
    assert "nothing is blocked" in reports[0].text
    narrating = [e for e in events if e.kind == "status" and e.text == "narrating"]
    assert len(narrating) == len(reports)  # one call per report, no idle polling calls


def test_narrator_off(tmp_path, monkeypatch):
    company, events = _run_company(tmp_path, monkeypatch, env={"TRIAD_FAKE_PLAN": "software"},
                                   cfg=fast_config(narrator="off", narrator_interval=0.2))
    assert not [e for e in events if e.kind == "progress"]


def test_summary_view_hides_the_noise(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAD_FAKE_DELAY", "0")
    monkeypatch.setenv("TRIAD_FAKE_PLAN", "software")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from triad.tui import TriadApp

    def pane(app, name):
        return "\n".join("".join(seg.text for seg in line) for line in app.query_one(name).lines)

    async def go():
        app = TriadApp(tmp_path / "ws", fast_config(narrator_interval=0.3), demo=True)
        (tmp_path / "ws").mkdir()
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.press(*"build a word counter", "enter")
            for _ in range(200):
                await pilot.pause(0.05)
                if app.mode == "review":
                    break
            await pilot.press("enter")
            for _ in range(600):
                await pilot.pause(0.05)
                if app.mode == "idle":
                    break
            summary = pane(app, "#summary")
            assert "Delivered" in summary and "Lead review of Build" in summary
            assert "checks team chat" not in summary and "write wordcount" not in summary  # no raw activity
            await pilot.press("ctrl+o")
            await pilot.pause(0.3)
            assert app.view == "detail" and "write wordcount/core.py" in pane(app, "#log")

    asyncio.run(go())


# ------------------------------------------------------------------ resuming and follow-ups

def _interrupt_after(tmp_path, monkeypatch, phase_prefix):
    """Run the demo and kill it (like closing the app) when a phase starts."""
    monkeypatch.setenv("TRIAD_FAKE_DELAY", "0")

    async def approve(plan):
        return "approve", ""

    async def go():
        holder = {}

        def on_event(e):
            if e.kind == "phase" and e.text.startswith(phase_prefix) and "task" in holder:
                holder["task"].cancel()

        company = Company(tmp_path, fast_config(), on_event, approve, demo=True)
        holder["task"] = asyncio.create_task(company.run("build a word counter"))
        with pytest.raises(asyncio.CancelledError):
            await holder["task"]

    asyncio.run(go())
    return Company.load_state(tmp_path)


def test_interrupted_project_resumes_without_redoing_finished_stages(tmp_path, monkeypatch):
    state = _interrupt_after(tmp_path, monkeypatch, "Stage 3/")
    assert state["status"] == "interrupted" and Company.resumable(state)
    assert [h["name"] for h in state["history"]] == ["Background research", "Design debate"]

    events = []

    async def never(plan):
        raise AssertionError("a resumed project must not be re-planned")

    company = Company(tmp_path, fast_config(), events.append, never, demo=True)
    asyncio.run(company.run(resume=state))
    phases = [e.text for e in events if e.kind == "phase"]
    assert phases[0] == "Resuming wordcount" and "Brainstorm" not in phases and "Planning" not in phases
    stage_phases = [p for p in phases if p.startswith("Stage ")]
    assert stage_phases == ["Stage 3/4 · Simulate: Usage simulation", "Stage 4/4 · Build: Build"]
    assert [h["type"] for h in company.history] == ["research", "debate", "simulate", "build"]
    assert Company.load_state(tmp_path)["status"] == "done" and (tmp_path / "DELIVERY.md").exists()
    chat = [e.text for e in events if e.kind == "chat"]
    assert any("Resuming after an interruption" in t for t in chat)


def test_follow_up_request_builds_on_the_finished_project(tmp_path, monkeypatch):
    _run_company(tmp_path, monkeypatch, env={"TRIAD_FAKE_PLAN": "software"})
    assert Company.load_state(tmp_path)["status"] == "done"
    company, events = _run_company(tmp_path, monkeypatch, env={"TRIAD_FAKE_PLAN": "software"})
    assert company.prior and company.prior["plan"]["project_name"] == "wordcount"
    assert list((tmp_path / ".triad" / "history").glob("*.json"))  # the earlier project is archived
    from triad import prompts as P
    assert "EARLIER PROJECT" in P.brainstorm("claude", TEAM, "add a --json flag", company.prior)


def test_cancelled_plan_is_not_offered_for_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAD_FAKE_DELAY", "0")
    decisions = [("revise", "smaller please"), ("cancel", "")]

    async def answer(plan):
        return decisions.pop(0)

    from triad.company import Cancelled
    company = Company(tmp_path, fast_config(), lambda e: None, answer, demo=True)
    with pytest.raises(Cancelled):
        asyncio.run(company.run("build a word counter"))
    assert not Company.resumable(Company.load_state(tmp_path))


def test_tui_offers_to_resume(tmp_path, monkeypatch):
    state = _interrupt_after(tmp_path, monkeypatch, "Stage 4/")
    assert Company.resumable(state)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from triad.tui import TriadApp

    def pane(app):
        return "\n".join("".join(seg.text for seg in line) for line in app.query_one("#summary").lines)

    async def go():
        app = TriadApp(tmp_path, fast_config(), demo=True)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause(0.3)
            assert "Unfinished project" in pane(app) and "next: stage 4/4" in pane(app)
            await pilot.press("enter")
            for _ in range(400):
                await pilot.pause(0.05)
                if app.mode == "idle" and "Delivered" in pane(app):
                    break
            assert "Resuming wordcount" in pane(app) and "Delivered" in pane(app)

    asyncio.run(go())
    assert Company.load_state(tmp_path)["status"] == "done"
