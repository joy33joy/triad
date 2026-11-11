"""triad: a small AI company (Claude Code, Codex, Antigravity) in your terminal.

  triad                         open the interactive app in the current folder
  triad "build me a ..."        open the app and start that project right away
  triad run "build me a ..."    plain streaming output, no full-screen UI (add --yes to skip plan review)
  triad resume                  continue an unfinished project in this folder (plain output)
  triad doctor [--ping]         check which agent CLIs are installed and signed in
  triad config [KEY VALUE]      show or change settings (e.g. `triad config lead codex`)

Options: -C DIR (workspace, default: current folder), --demo (offline fake agents, no quota used),
         --team claude,codex,claude (who's on the team for this run; any mix and size)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from . import __version__
from .agents import AGENTS, COLORS, DISPLAY, EXECUTABLES, Usage, color, demo_prefix, display, installed, name_members, provider_of, run_agent
from .company import Cancelled, Company, Event
from .config import AgentConfig, Config, config_path


def _check_workspace(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path == Path.home().resolve() or path == Path("/"):
        sys.exit(f"Refusing to use {path} as a workspace. Make a project folder first:\n  mkdir my-app && cd my-app && triad")
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------- headless

ANSI = {"claude": "38;5;173", "codex": "38;5;36", "antigravity": "38;5;69", "triad": "38;5;176", "user": "38;5;179",
        "lead": "38;5;167"}


def _c(agent: str, text: str) -> str:
    code = ANSI.get(agent) or ANSI.get(provider_of(agent), "0")
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


VERBOSE = False
QUIET_KINDS = {"activity", "chat", "log", "status"}


def print_event(e: Event) -> None:
    """Default output is the summary (milestones, reports, lead); -v prints everything."""
    if not VERBOSE:
        if e.kind in QUIET_KINDS or (e.kind == "error" and "usage limit" not in e.text and "not signed in" not in e.text):
            return
        if e.kind == "turn":
            print(f"{_c(e.agent, '  ┌ ' + (e.data or {}).get('title', ''))}")
            return
    name = display(e.agent)
    if e.kind == "phase":
        print(f"\n{_c('triad', '━━ ' + e.text + ' ━━')}")
    elif e.kind == "activity":
        if e.data == "proposal":
            PROPOSALS[e.agent] = e.text
            print(f"{_c(e.agent, f'{name:>11}')} │ 💡 shared an approach")
        elif e.data == "thought":
            pass  # internal reasoning stays out of the feed
        elif e.data == "text":
            first = e.text.strip().splitlines()[0] if e.text.strip() else ""
            print(f"{_c(e.agent, f'{name:>11}')} │ {first[:160]}")
        else:
            print(f"{_c(e.agent, f'{name:>11}')} │ " + (f"\033[2m{e.text}\033[0m" if sys.stdout.isatty() else e.text))
    elif e.kind == "chat":
        to = e.data.get("to", "all") if isinstance(e.data, dict) else "all"
        kind = e.data.get("kind", "chat") if isinstance(e.data, dict) else "chat"
        icon = {"question": "❓", "checkin": "🧭", "answer": "↩", "direction": "🧭", "review": "🔍"}.get(kind, "💬")
        print(f"{_c(e.agent, f'{name:>11}')} {icon} → {to}: {e.text}")
    elif e.kind == "turn":
        title = (e.data or {}).get("title", display(e.agent))
        print(f"\n{_c(e.agent, '┌ ' + title)}\n  " + e.text.strip().replace("\n", "\n  "))
    elif e.kind == "lead_reply":
        print(f"{_c('lead', '      lead')} 💬 → you: {e.text}")
        for act in (e.data or {}).get("actions") or []:
            print(f"{'':>13}→ {act}")
    elif e.kind == "progress":
        print(f"{_c('triad', '   progress')} 📰 {e.text.strip()}")
    elif e.kind == "staff":
        d = e.data or {}
        for m in d.get("added", []):
            print(f"{_c('lead', '      lead')} 👥 hired {display(m)}: {d.get('reasons', {}).get(m, '')}")
        for m in d.get("benched", []):
            print(f"{_c('lead', '      lead')} 👥 benched {display(m)}")
    elif e.kind == "adapt":
        d = e.data or {}
        print(f"{_c('lead', '      lead')} 🔀 changed the remaining workflow:")
        for line in sum((format_stage(d['from'] + i, st) for i, st in enumerate(d["stages"])), []) or ["  (no more stages)"]:
            print("  " + line)
    elif e.kind == "review":
        d = e.data or {}
        if d.get("kind") == "direction":
            print(f"{_c('lead', '      lead')} 🧭 direction check: {e.text}")
            items = d.get("notes") or {}
        else:
            print(f"{_c('lead', '      lead')} 🔍 review of {d.get('stage', '')} (round {d.get('round', '')}): {e.text}. {d.get('summary', '')}")
            items = d.get("feedback") or {}
        for agent, note in items.items():
            print(f"{'':>13}→ {display(agent)}: {note}")
    elif e.kind == "halt":
        target = "the whole team (replanning)" if e.agent == "all" else display(e.agent)
        print(f"{_c('lead', '      lead')} ✋ halted {target}: {e.text}")
    elif e.kind in ("log", "error"):
        print(f"{_c('triad', '      triad')} │ {e.text}")
    elif e.kind == "plan":
        LAST_PLAN.clear()
        LAST_PLAN.update(e.data)
        print(format_brief(e.data, e.agent))
    elif e.kind == "done":
        print(f"\n{_c('triad', '━━ Delivered ━━')}\n{e.text}")


def format_stage(i: int, st: dict) -> list[str]:
    lines = [f"  {i}. {st['name']} [{st['type']}] {st.get('goal', '')}".rstrip()]
    if st["type"] in ("research", "build"):
        for a in st["assignments"]:
            lines.append(f"       • {display(a['agent'])}, {a['role']}: {a['task']}")
            if a["owns"]:
                lines.append(f"         owns {', '.join(a['owns'])}")
    elif st["type"] == "debate":
        lines.append(f"       question: {st['topic']} ({st['rounds']} rounds)")
        for agent, pos in st["positions"].items():
            lines.append(f"       • {display(agent)} argues: {pos}")
    else:
        for agent, actor in st["actors"].items():
            lines.append(f"       • {display(agent)} plays {actor}")
        lines.append(f"       game master: {display(st['game_master'])}, {st['turns']} turns")
        for sc in st["scenarios"]:
            lines.append(f"       🎲 {sc['name']}: {sc['setup']}")
    if st.get("skills"):
        lines.append(f"       skills: {', '.join(st['skills'])}")
    return lines


PROPOSALS: dict[str, str] = {}
LAST_PLAN: dict = {}


def format_brief(plan: dict, lead: str) -> str:
    lines = [f"\n📋 {display(lead)}'s plan: {plan['project_name']}", "", plan.get("client_brief", "")]
    if plan.get("questions"):
        lines += ["", "What I need from you:"] + [f"  {i}. {q}" for i, q in enumerate(plan["questions"], 1)]
        lines += ["  (answer in one message, e.g. \"1: yes, 2: Korea only\"; anything you skip, the team decides)"]
    if plan.get("team"):
        lines += ["", "Team: " + ", ".join(display(m) for m in plan["team"])]
    return "\n".join(lines)


def format_plan(plan: dict) -> str:
    lines = [f"\n📋 {plan['project_name']}: {plan['summary']}", f"Deliverable: {plan['deliverable']}", "",
             "Conventions:", "  " + plan["architecture"].replace("\n", "\n  "), "", "Workflow:"]
    for i, st in enumerate(plan["stages"], 1):
        lines += format_stage(i, st)
    if plan.get("verify"):
        lines.append(f"\nVerify with: {plan['verify']}")
    return "\n".join(lines)


async def _headless(workspace: Path, request: str, cfg: Config, demo: bool, yes: bool,
                    team: list[str] | None = None, resume: dict | None = None) -> int:
    sys.stdout.reconfigure(line_buffering=True)  # live output even when piped to a file
    async def ask(plan: dict) -> tuple[str, str]:
        if yes:
            return "approve", ""
        while True:
            answer = await asyncio.to_thread(
                input, "\nEnter = start · type answers or changes · d = full plan · n = cancel: ")
            answer = answer.strip()
            if answer.lower() not in ("d", "details"):
                break
            for agent, text in PROPOSALS.items():
                print(f"\n{display(agent)}'s approach:\n  " + text.strip().replace("\n", "\n  "))
            print(format_plan(plan))
        if answer.lower() in ("", "y", "yes"):
            return "approve", ""
        if answer.lower() in ("n", "no", "q"):
            return "cancel", ""
        return "revise", answer

    company = Company(workspace, cfg, print_event, ask, demo=demo, team_spec=team)
    if not resume:  # a resumed project keeps its saved team, restored when it starts
        print(f"Team: {', '.join(display(m) for m in company.team) or 'nobody'}")
    else:
        print(f"Team: {', '.join(display(m) for m in resume.get('team', [])) or 'nobody'}")
    try:
        await company.run(request, resume=resume)
    except Cancelled:
        print("Cancelled.")
        return 1
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("\nUsage this run: " + ", ".join(
        f"{display(m)} {u.calls} calls / {u.tokens:,} tokens" for m, u in company.member_usage.items() if u.calls))
    return 0


# ---------------------------------------------------------------- doctor / config

def doctor(cfg: Config, ping: bool) -> int:
    ok = 0
    for agent in AGENTS:
        ac = cfg.agents[agent]
        exe = ac.executable or EXECUTABLES[agent]
        path = installed(agent, ac.executable)
        if not ac.enabled:
            print(f"  ○ {display(agent):<12} disabled in config")
            continue
        if not path:
            print(f"  ✗ {display(agent):<12} `{exe}` not found on PATH")
            continue
        version = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30).stdout.strip().splitlines()
        line = f"  ✓ {display(agent):<12} {path} {version[0] if version else ''}"
        if ping:
            usage = Usage()
            r = asyncio.run(run_agent(agent, "think", Path.cwd(), "Reply with exactly: ok", usage=usage,
                                      model=ac.model, executable=ac.executable, timeout=180))
            line += "  — responds ✓" if r.ok else f"  — ✗ {r.status}: {r.error[:200]}"
            if usage.quota:
                line += "  (" + ", ".join(f"{k} used {v:.0%}" for k, v in usage.quota.items()) + ")"
            ok += r.ok
        else:
            ok += 1
        print(line)
    print(f"\nLead: {cfg.lead}.  Config: {config_path()}")
    if not ping:
        print("Run `triad doctor --ping` to send each agent a tiny test prompt (checks sign-in).")
    return 0 if ok else 1


def config_cmd(cfg: Config, args: list[str]) -> int:
    if not args:
        print(f"# {config_path()}")
        from dataclasses import asdict
        print(json.dumps(asdict(cfg), indent=2))
        print("\nExamples: triad config team claude,codex,claude | triad config lead codex | triad config max_team 4 | "
              "triad config codex.model gpt-6-astra | triad config antigravity.enabled false")
        return 0
    if len(args) != 2:
        sys.exit("usage: triad config KEY VALUE")
    key, value = args
    if "." in key:
        agent, field = key.split(".", 1)
        if agent not in AGENTS or field not in AgentConfig.__dataclass_fields__:
            sys.exit(f"unknown setting {key}")
        setattr(cfg.agents[agent], field, value.lower() in ("1", "true", "yes", "on") if field == "enabled" else value)
    else:
        if not hasattr(cfg, key) or key == "agents":
            sys.exit(f"unknown setting {key}")
        if key == "lead" and provider_of(value) not in AGENTS:
            sys.exit(f"lead must be a member like claude, codex-2 or antigravity")
        current = getattr(cfg, key)
        if key == "team":
            spec = [] if value in ("", "default", "reset") else [x.strip() for x in value.split(",") if x.strip()]
            if spec and not name_members(spec):
                sys.exit("team is a comma-separated list of claude/codex/antigravity (optionally provider:model)")
            cfg.team = spec
            print(f"team: {', '.join(n for n, _, _ in name_members(spec)) or 'default (one of each installed CLI)'}")
            print(f"saved {cfg.save()}")
            return 0
        setattr(cfg, key, value.lower() in ("1", "true", "yes", "on") if isinstance(current, bool) else type(current)(value))
    print(f"saved {cfg.save()}")
    return 0


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="triad", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-C", "--dir", default=".", help="workspace folder (default: current folder)")
    ap.add_argument("--demo", action="store_true", help="use offline fake agents (no accounts, no quota)")
    ap.add_argument("--yes", "-y", action="store_true", help="approve the plan automatically")
    ap.add_argument("-v", "--verbose", action="store_true", help="(run) print every action, not just the summary")
    ap.add_argument("--team", help="members for this run, e.g. claude,codex,claude or codex:gpt-6-astra,claude")
    ap.add_argument("--ping", action="store_true", help="(doctor) send each agent a test prompt")
    ap.add_argument("--version", action="version", version=f"triad {__version__}")
    ap.add_argument("words", nargs="*", help="run | doctor | config | a project request")
    args = ap.parse_intermixed_args(argv)
    cfg = Config.load()
    words = args.words
    command = words[0] if words and words[0] in ("run", "doctor", "config", "resume") else ""
    rest = words[1:] if command else words

    if command == "doctor":
        return doctor(cfg, args.ping)
    if command == "config":
        return config_cmd(cfg, rest)
    workspace = _check_workspace(Path(args.dir))
    request = " ".join(rest).strip()
    team = [x.strip() for x in args.team.split(",") if x.strip()] if args.team else None
    if team is not None and not name_members(team):
        sys.exit("--team is a comma-separated list of claude/codex/antigravity (optionally provider:model)")
    if command == "resume":
        global VERBOSE
        VERBOSE = args.verbose
        state = Company.load_state(workspace)
        if not Company.resumable(state):
            sys.exit("Nothing to resume in this folder.")
        print(f"Resuming {state['plan'].get('project_name')} after {len(state.get('history', []))} finished stage(s).")
        return asyncio.run(_headless(workspace, "", cfg, args.demo, True, team, resume=state))
    if command == "run":
        VERBOSE = args.verbose
        if not request:
            sys.exit('usage: triad run "describe the project"')
        return asyncio.run(_headless(workspace, request, cfg, args.demo, args.yes, team))
    from .tui import TriadApp
    TriadApp(workspace, cfg, demo=args.demo, initial=request, team_spec=team).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
