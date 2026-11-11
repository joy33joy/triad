"""The company: one request in, one cohesive product out.

    request ─▶ brainstorm (all three, in parallel, read-only)
            ─▶ plan (lead merges the ideas, splits work evenly)            ─▶ client approves
            ─▶ build (all three in parallel, talking on the team chat)
                  ├─ each checks in early with a plan of attack ─▶ lead's direction check (once)
                  ├─ anyone can `ask` the lead; questions are batched into one lead call
                  └─ the lead can halt one engineer (restart with a correction) or everyone (replan)
            ─▶ sync (each fits their part to the others' until all READY)
            ─▶ review loop: lead reviews cohesion ─▶ targeted fixes by only those who need them ─▶ re-review
            ─▶ final assembly by the lead (README + DELIVERY.md)

Every lead intervention is event-driven and capped, so supervision happens when it is needed and
doesn't burn usage when it isn't. If someone runs out of usage, the teammate with the most headroom
picks up their area.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import prompts
from .agents import AGENTS, DISPLAY, Activity, Result, Usage, demo_prefix, display, installed, name_members, provider_of, run_agent
from .bus import Board
from .config import Config
from .skills_loader import load_skills


@dataclass
class Event:
    kind: str            # phase | activity | chat | status | plan | review | halt | log | error | done
    agent: str = ""
    text: str = ""
    data: Any = None


Emit = Callable[[Event], Awaitable[None] | None]
# Returns ("approve", "") | ("revise", feedback) | ("cancel", "")
AskPlan = Callable[[dict[str, Any]], Awaitable[tuple[str, str]]]

DEFAULT_EXCLUDES = [".triad/", "__pycache__/", "*.pyc", "node_modules/", ".venv/", "venv/", ".pytest_cache/",
                    ".mypy_cache/", "*.egg-info/", ".DS_Store"]
PromptFor = Callable[["Area"], Callable[[str, str], str]]


class Cancelled(Exception):
    pass


class Replan(Exception):
    def __init__(self, reason: str, source: str = "lead"):
        super().__init__(reason)
        self.reason, self.source = reason, source  # source: "lead" (lead halted) or "pivot" (client changed direction)


@dataclass
class Area:
    """One slice of the project, owned by one agent (or a stand-in if they run out)."""
    id: str                       # the agent it was planned for
    assignment: dict[str, Any]
    worker: str                   # who is currently doing it
    handoff: str = ""
    status: str = "pending"
    task: asyncio.Task | None = None
    running: bool = False
    redirect: str = ""            # the lead's correction, applied when the area restarts
    halts: int = 0

    @property
    def label(self) -> str:
        return self.id if self.worker == self.id else f"{self.id}'s area (covered by {self.worker})"


@dataclass
class Company:
    workspace: Path
    config: Config
    emit: Emit
    ask_plan: AskPlan
    demo: bool = False
    usage: dict[str, Usage] = field(default_factory=lambda: {a: Usage() for a in AGENTS})  # per provider (subscription)
    team_spec: list[str] | None = None  # overrides config.team for this company

    def __post_init__(self) -> None:
        self.workspace = self.workspace.resolve()
        self.members: dict[str, dict[str, str]] = {}   # name -> {"provider", "model"}
        self.member_usage: dict[str, Usage] = {}       # per member: calls / tokens / time
        self.busy: dict[str, int] = {}
        for name, provider, model in self.initial_members():
            self._add_member(name, provider, model)
        self.team = list(self.members)                 # active members (benched ones are removed)
        self.allowed_providers = sorted({m["provider"] for m in self.members.values()})
        self.board = Board(self.workspace, self.team)
        self._watcher: asyncio.Task | None = None
        self._snapshots = False
        self._branch_pending = False
        self._replan_reason = ""
        self.replans = 0
        self.lead_answers = 0
        self.request = ""
        self.plan: dict[str, Any] = {}
        self.stage: dict[str, Any] = {}
        self.history: list[dict[str, Any]] = []
        self.skills = load_skills(self.config)
        self.last_activity: dict[str, str] = {}
        self.status = "planning"
        self.delivery = ""
        self.prior: dict[str, Any] | None = None   # a finished earlier project in this folder (for follow-ups)
        self.resume_index = 0
        self.feed: list[str] = []          # terse running log the reporter summarizes
        self._feed_seen = 0
        self.progress = ""                 # the latest progress report
        self._narrator_task: asyncio.Task | None = None
        self.stage_index = 0
        self.active_areas: list[Area] = []
        self._stage_task: asyncio.Task | None = None
        self._pivot = ""
        self._client_lock = asyncio.Lock()

    # ------------------------------------------------------------------ team

    def _usable(self, provider: str) -> bool:
        ac = self.config.agents[provider]
        return ac.enabled and (self.demo or bool(installed(provider, ac.executable)))

    def initial_members(self) -> list[tuple[str, str, str]]:
        spec = self.team_spec if self.team_spec is not None else self.config.team
        if not spec:
            spec = [a for a in AGENTS if self._usable(a)]
        return [(n, p, m) for n, p, m in name_members(spec) if self._usable(p)]

    def available_team(self) -> list[str]:
        return list(self.team)

    def _add_member(self, name: str, provider: str, model: str = "") -> None:
        self.members[name] = {"provider": provider, "model": model}
        self.member_usage.setdefault(name, Usage())
        self.busy.setdefault(name, 0)

    def next_name(self, provider: str, taken: list[str] | None = None) -> str:
        taken = list(self.members) + list(taken or [])
        n = 1
        while (provider if n == 1 else f"{provider}-{n}") in taken:
            n += 1
        return provider if n == 1 else f"{provider}-{n}"

    def propose_staffing(self, data: dict[str, Any], team: list[str]) -> tuple[list[str], list[tuple[str, str, str]]]:
        """Apply a lead's {"staffing": {"add": [...], "bench": [...]}} to a team list (without committing).
        Returns (new team, hires as (name, provider, reason))."""
        staffing = data.get("staffing") if isinstance(data.get("staffing"), dict) else {}
        new_team = list(team)
        for b in staffing.get("bench") or []:
            who = _canon(str(b), new_team)
            if who and who != "all" and len(new_team) > 1:
                new_team.remove(who)
        hires: list[tuple[str, str, str]] = []
        for h in staffing.get("add") or []:
            provider = str((h.get("provider") if isinstance(h, dict) else h) or "").lower().strip()
            provider = {"gemini": "antigravity", "agy": "antigravity"}.get(provider, provider)
            if provider not in self.allowed_providers or len(new_team) >= max(1, self.config.max_team):
                continue
            benched = [m for m in self.members if self.members[m]["provider"] == provider and m not in new_team]
            name = benched[0] if benched else self.next_name(provider, [n for n, _, _ in hires])
            reason = str(h.get("reason", "")) if isinstance(h, dict) else ""
            new_team.append(name)
            hires.append((name, provider, reason))
        return new_team, hires

    async def commit_team(self, new_team: list[str], hires: list[tuple[str, str, str]], by: str) -> None:
        for name, provider, _ in hires:
            if name not in self.members:
                self._add_member(name, provider, self.config.agents[provider].model)
        added = [n for n in new_team if n not in self.team]
        benched = [n for n in self.team if n not in new_team]
        self.team = list(new_team)
        self.board.set_team(self.team)
        self.save_state()
        if added or benched:
            reasons = {n: r for n, _, r in hires}
            await self._emit(Event("staff", by, "", {"added": added, "benched": benched, "reasons": reasons,
                                                     "team": list(self.team)}))

    def staffing_brief(self) -> str:
        return prompts.staffing_text(self.team, self.allowed_providers, self.config.max_team,
                                     {p: self.next_name(p) for p in self.allowed_providers})

    def lead(self, exclude: tuple[str, ...] = ()) -> str | None:
        want = self.config.lead
        same_provider = [m for m in self.team if self.members[m]["provider"] == provider_of(want)]
        order = ([want] if want in self.team else []) + same_provider + list(self.team)
        for a in order:
            if a in self.team and a not in exclude and self.usage[self.members[a]["provider"]].available():
                return a
        return None

    def substitute(self, exclude: set[str]) -> str | None:
        """The available member with the most headroom: idle first, then the least-used subscription."""
        options = [a for a in self.team if a not in exclude and self.usage[self.members[a]["provider"]].available()]
        return min(options, key=lambda a: (self.busy[a], self.usage[self.members[a]["provider"]].tokens)) if options else None

    def member_available(self, name: str) -> bool:
        return name in self.members and self.usage[self.members[name]["provider"]].available()

    async def _emit(self, event: Event) -> None:
        self._record(event)
        maybe = self.emit(event)
        if asyncio.iscoroutine(maybe):
            await maybe

    async def say(self, text: str, to: str = "all") -> None:
        """The client (user) posts on the team board while work is underway."""
        if not self.board.path.exists():
            self.board.install()
        self.board.post("user", to, text)

    # ------------------------------------------------------------------ one turn

    async def turn(self, agent: str, mode: str, prompt: str, *, as_lead: bool = False, narrating: bool = False) -> Result:
        member = self.members[agent]
        provider = member["provider"]
        ac = self.config.agents[provider]
        sub, mine = self.usage[provider], self.member_usage[agent]
        self.busy[agent] += 1
        await self._emit(Event("status", agent, "narrating" if narrating else "reviewing" if as_lead
                               else "working" if mode == "work" else "thinking"))

        async def on_activity(act: Activity) -> None:
            if narrating:
                return  # the reporter's own steps are not news
            kind = "thought" if act.kind == "text" and (as_lead or mode == "think") else act.kind
            if not as_lead and act.text.strip():
                self.last_activity[act.agent] = act.text.strip().splitlines()[0][:160]
            await self._emit(Event("activity", act.agent, act.text, kind))

        try:
            result = await run_agent(
                agent, mode, self.workspace, prompt, usage=sub, on_activity=on_activity,
                model=member["model"] or ac.model, executable=ac.executable, timeout=self.config.turn_timeout,
                env={"TRIAD_AGENT": agent, "TRIAD_PROVIDER": provider}, argv_prefix=demo_prefix() if self.demo else None,
                network=self.config.codex_network, web=self.config.web and not narrating, member_usage=mine)
        finally:
            self.busy[agent] -= 1
            await self._emit(Event("status", agent, "idle" if self.busy[agent] == 0 else "working"))
        if not result.ok:
            reason = sub.exhausted_reason or result.status
            await self._emit(Event("error", agent, f"{display(agent)} failed ({reason}): {result.error[:400]}"))
        return result

    async def area_turn(self, area: Area, make_prompt: Callable[[str, str], str], mode: str = "work") -> Result:
        """Run a turn for an area; if its worker is out of usage or fails, hand it to a teammate."""
        tried: set[str] = set()
        while True:
            worker = area.worker if area.worker not in tried and area.worker in self.team and self.member_available(area.worker) \
                else self.substitute(tried)
            if worker is None:
                return Result(area.worker, False, status="error", error="no teammate available")
            if worker != area.worker:
                await self._emit(Event("log", "triad", f"{display(worker)} is taking over {area.id}'s area from {display(area.worker)}."))
                area.worker = worker
            note = "" if worker == area.id else (
                f"NOTE: you are covering this area for {display(area.id)}, who is unavailable. "
                f"Pick up from whatever is already in the workspace.")
            result = await self.turn(worker, mode, make_prompt(worker, note))
            if result.ok or result.status == "cancelled":
                return result
            tried.add(worker)

    async def lead_call(self, prompt_for_lead: Callable[[str], str]) -> tuple[str, dict[str, Any] | None]:
        """One read-only call to the lead (falling back to another teammate); returns its JSON decision."""
        tried: tuple[str, ...] = ()
        while (lead := self.lead(exclude=tried)) is not None:
            result = await self.turn(lead, "think", prompt_for_lead(lead), as_lead=True)
            if result.ok:
                return lead, parse_json_object(result.text)
            tried += (lead,)
        return self.config.lead, None

    # ------------------------------------------------------------------ the project

    # ------------------------------------------------------------------ saved state (resume / follow-ups)

    @staticmethod
    def load_state(workspace: Path) -> dict[str, Any] | None:
        try:
            state = json.loads((Path(workspace) / ".triad" / "state.json").read_text())
            return state if isinstance(state, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def resumable(state: dict[str, Any] | None) -> bool:
        return bool(state and state.get("status") in ("running", "interrupted") and state.get("plan", {}).get("stages"))

    def save_state(self, status: str | None = None) -> None:
        if status:
            self.status = status
        if not self.plan:
            return
        state = {"version": 1, "status": self.status, "request": self.request, "plan": self.plan,
                 "history": self.history, "team": self.team, "members": self.members, "replans": self.replans,
                 "lead_answers": self.lead_answers, "stage_index": self.stage_index, "progress": self.progress,
                 "delivery": self.delivery, "updated": time.time()}
        path = self.board.dir / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(path)

    def _restore(self, state: dict[str, Any]) -> None:
        self.request, self.plan = state["request"], state["plan"]
        self.history = list(state.get("history", []))
        self.replans, self.lead_answers = int(state.get("replans", 0)), int(state.get("lead_answers", 0))
        self.progress = str(state.get("progress", ""))
        for name, m in (state.get("members") or {}).items():
            if name not in self.members and self._usable(m.get("provider", "")):
                self._add_member(name, m["provider"], m.get("model", ""))
        team = [m for m in state.get("team", []) if m in self.members]
        if team:
            self.team = team
        self.allowed_providers = sorted(set(self.allowed_providers) | {self.members[m]["provider"] for m in self.team})

    async def run(self, request: str = "", *, resume: dict[str, Any] | None = None) -> str:
        """Run a project. `resume` is a saved state to continue; a finished project in the folder
        becomes context for the new request (a follow-up)."""
        previous = self.load_state(self.workspace)
        if resume:
            self._restore(resume)
        elif previous and previous.get("plan"):
            if previous.get("status") == "done":
                self.prior = previous
            archive = self.workspace / ".triad" / "history"
            archive.mkdir(parents=True, exist_ok=True)
            (archive / f"{int(previous.get('updated', time.time()))}.json").write_text(json.dumps(previous, indent=2))
        if not self.team:
            raise RuntimeError("No agent CLIs found. Install and sign in to claude, codex and/or agy (run `triad doctor`).")
        self.board = Board(self.workspace, self.team, self.lead() or self.team[0])
        self.board.install()
        if not resume:
            self.board.reset()  # a resumed project keeps its team chat
        git_note = self._setup_git()
        if git_note:
            await self._emit(Event("log", "triad", git_note))
        self._watcher = asyncio.create_task(self._watch_board())
        self._narrator_task = asyncio.create_task(self._narrate())
        try:
            return await self._run(request or self.request, resumed=bool(resume))
        except Cancelled:
            self.save_state("cancelled")
            raise
        finally:
            if self.status != "done":
                self.save_state("interrupted")
            self._watcher.cancel()
            self._narrator_task.cancel()
            await self._flush_board()

    async def _run(self, request: str, resumed: bool = False) -> str:
        team, cfg = self.team, self.config
        self.request = request
        await self._emit(Event("log", "triad", f"Team: {', '.join(display(a) for a in team)}. Workspace: {self.workspace}"))
        proposals: dict[str, str] = {}
        start = 0
        if resumed:
            start = len(self.history)
            self.resume_index = start + 1
            await self._emit(Event("phase", text=f"Resuming {self.plan.get('project_name', 'project')}",
                                   data={"done": [h["name"] for h in self.history]}))
            self.board.post("triad", "all", f"Resuming after an interruption at stage {start + 1}. "
                            "Work from the earlier session is in the workspace; pick up where it left off.")
            self.save_state("running")
        else:
            # 1. Brainstorm
            if cfg.brainstorm and len(team) > 1:
                await self._emit(Event("phase", text="Brainstorm"))
                results = await asyncio.gather(*(self.turn(a, "think", prompts.brainstorm(a, team, request, self.prior))
                                                 for a in team))
                proposals = {r.agent: r.text for r in results if r.ok}
                for agent, text in proposals.items():
                    await self._emit(Event("activity", agent, text, "proposal"))

            # 2. The lead designs the workflow; the client approves it
            await self._emit(Event("phase", text="Planning"))
            plan = await self._approved_plan(request, proposals)
            branch_note = self._branch(plan.get("project_name", "project"))
            if branch_note:
                await self._emit(Event("log", "triad", branch_note))

        # 3. Run the stages; the lead reviews each one and may adapt what comes next
        open_notes = ""
        while True:
            try:
                i = start
                while i < len(self.plan["stages"]):
                    stage = self.plan["stages"][i]
                    self.stage_index = i + 1
                    self._stage_task = asyncio.create_task(self._run_stage(i + 1, stage))
                    try:
                        open_notes = await self._stage_task
                    except asyncio.CancelledError:
                        me = asyncio.current_task()
                        if self._pivot and not (me and me.cancelling()):  # the client changed direction
                            reason, self._pivot = self._pivot, ""
                            raise Replan(reason, source="pivot")
                        raise
                    finally:
                        self._stage_task = None
                    i += 1
                break
            except Replan as rp:
                self.replans += 1
                self._snapshot("triad: work before replan")
                await self._emit(Event("phase", text="Replanning" if rp.source != "pivot" else "Changing direction"))
                await self._approved_plan(request, proposals, rp.reason, self.plan, source=rp.source)
                start = 0
                self.board.post("triad", "all", "The plan was revised; the team restarts from the new workflow.")

        # 4. Final assembly by the lead
        plan = self.plan
        await self._emit(Event("phase", text="Final assembly"))
        before = await self._verify(plan)
        final = Result("", False)
        for _ in range(2):
            lead = self.lead(exclude=(final.agent,) if final.agent else ())
            if not lead:
                break
            if final.agent:
                await self._emit(Event("log", "triad", f"{display(lead)} is doing final assembly instead."))
            final = await self.turn(lead, "work", prompts.integrate(
                lead, team, request, plan, self.history, self.board.transcript(), before, open_notes))
            if final.ok:
                break
        after = await self._verify(plan)
        self._snapshot("triad: delivery")

        summary = final.text if final.ok else "Final assembly could not be completed; the team's work is in the workspace."
        if after:
            summary += "\n\n**Verification**\n```\n" + after[-1500:].strip() + "\n```"
        self.delivery = summary
        self.save_state("done")
        await self._emit(Event("done", final.agent or self.config.lead, summary, {"plan": plan, "history": self.history}))
        return summary

    async def _approved_plan(self, request: str, proposals: dict[str, str], feedback: str = "",
                             previous: dict[str, Any] | None = None, source: str = "client") -> dict[str, Any]:
        client_input = list((previous or {}).get("client_input", []))
        plan = await self._make_plan(request, proposals, feedback, previous, source)
        plan["client_input"] = list(client_input)
        while True:
            decision, note = await self.ask_plan(plan)
            if decision == "approve":
                break
            if decision == "cancel":
                raise Cancelled("plan rejected")
            client_input.append(note)
            await self._emit(Event("phase", text="Revising plan"))
            plan = await self._make_plan(request, proposals, note, plan)
            plan["client_input"] = list(client_input)
        self.plan = plan
        self.history = []
        await self.commit_team(plan.get("team", self.team),
                               [(h["name"], h["provider"], h.get("reason", "")) for h in plan.get("hires", [])],
                               self.lead() or self.team[0])
        (self.board.dir / "plan.json").write_text(json.dumps(plan, indent=2))
        self.save_state("running")
        return plan

    # ------------------------------------------------------------------ stages

    async def _run_stage(self, index: int, stage: dict[str, Any]) -> str:
        """Run one stage, then the lead's gate. Returns review notes left for final assembly."""
        total = len(self.plan["stages"])
        self.stage = stage
        folder = stage_folder(index, stage)
        if stage["type"] == "research":  # use the folder the plan actually sends notes to, if they share one
            dirs = {str(Path(o).parent) for a in stage["assignments"] for o in a["owns"] if o.startswith("work/")}
            if len(dirs) == 1 and (d := dirs.pop()) not in (".", "work"):
                folder = d
        if stage["type"] != "build":
            (self.workspace / folder).mkdir(parents=True, exist_ok=True)
        await self._emit(Event("phase", text=f"Stage {index}/{total} · {stage['type'].title()}: {stage['name']}",
                               data={"stage": stage, "index": index}))
        areas: list[Area] = []
        if stage["type"] in ("research", "build"):
            areas = [Area(a["agent"], a, a["agent"]) for a in stage["assignments"]]
            await self._stage_work(index, stage, areas)
        elif stage["type"] == "debate":
            await self._stage_debate(index, stage, folder)
        else:
            await self._stage_simulate(index, stage, folder)
        self._snapshot(f"triad: stage {index} {stage['name']}")
        summary, notes = await self._gate(index, stage, folder, areas)
        if stage["type"] != "build":  # build stages speak through their artifacts; others get a written summary
            (self.workspace / folder / "SUMMARY.md").write_text(f"# {stage['name']} ({stage['type']})\n\n{summary}\n", encoding="utf-8")
        self.history.append({"index": index, "name": stage["name"], "type": stage["type"], "folder": folder, "summary": summary})
        self.save_state()
        self._snapshot(f"triad: stage {index} reviewed")
        return notes

    async def _stage_work(self, index: int, stage: dict[str, Any], areas: list[Area]) -> None:
        team, cfg = self.team, self.config
        lead = self.lead() or team[0]
        self.board.post("triad", "all", f"Stage {index} '{stage['name']}' ({stage['type']}) starts. Lead: {display(lead)}. "
                        "Check in with your approach early; ask the lead if something important is ambiguous.")
        history = list(self.history)
        resume = ("RESUMING: this stage was interrupted in an earlier session. Whatever is already in the workspace "
                  "for your assignment is your earlier work: check it, keep what is good, and finish the rest."
                  if index == self.resume_index else "")
        await self._work_round(areas, lambda area: lambda w, note: prompts.work(
            w, team, lead, self.request, self.plan, stage, index, area.assignment, history, self.skills,
            "\n".join(x for x in (note, resume) if x)), kind="build")
        if stage["type"] != "build" or len(areas) < 2:
            return
        for n in range(1, max(0, cfg.sync_rounds) + 1):  # peers fit their parts together
            if all(a.status == "READY" for a in areas):
                break
            await self._emit(Event("phase", text=f"Integration round {n}"))
            handoffs = {a.label: a.handoff or "(no report)" for a in areas}
            transcript = self.board.transcript()
            await self._work_round(areas, lambda area: lambda w, note: prompts.sync(
                w, team, lead, self.request, self.plan, stage, area.assignment, handoffs, transcript, n)
                + (f"\n{note}" if note else ""), kind="sync")

    async def _stage_debate(self, index: int, stage: dict[str, Any], folder: str) -> None:
        rounds = stage["rounds"]
        path = self.workspace / folder / "debate.md"
        transcript = ""
        with path.open("w", encoding="utf-8") as f:
            f.write(f"# Debate: {stage.get('topic') or stage['name']}\n\n")
            for agent, pos in stage["positions"].items():
                f.write(f"- **{display(agent)}** argues: {pos}\n")
        for r in range(1, rounds + 1):
            label = "Opening statements" if r == 1 else "Closing statements" if r == rounds else f"Rebuttals (round {r})"
            await self._emit(Event("log", "triad", f"Debate · {label}"))
            snapshot = transcript
            results = await asyncio.gather(*(self.turn_with_fallback(agent, "think", lambda w, a=agent: prompts.debate_turn(
                w, self.team, self.request, self.plan, stage, index, stage["positions"][a], r, rounds, snapshot,
                self.history, self.skills)) for agent in stage["positions"]))
            block = f"\n## {label}\n"
            positions = []
            for agent, res in zip(stage["positions"], results):
                if not res.ok:
                    continue
                text = res.text.strip()
                block += f"\n### {display(res.agent)} ({stage['positions'][agent][:80]})\n{text}\n"
                await self._emit(Event("turn", res.agent, text, {"kind": "debate", "title": f"{display(res.agent)} · {label}"}))
                m = re.search(r"POSITION:\s*\**\s*(HOLD|SHIFTED|CONVERGED)", text, re.I)
                positions.append(m.group(1).upper() if m else "HOLD")
            transcript += block
            with path.open("a", encoding="utf-8") as f:
                f.write(block)
            if r > 1 and positions and all(p == "CONVERGED" for p in positions):
                await self._emit(Event("log", "triad", "Everyone converged; ending the debate early."))
                break

    async def _stage_simulate(self, index: int, stage: dict[str, Any], folder: str) -> None:
        gm = stage["game_master"] if stage["game_master"] in self.team else (self.lead() or self.team[0])
        for scenario in stage["scenarios"]:
            path = self.workspace / folder / f"{_slug(scenario['name'])}.md"
            state, log = scenario["setup"], ""
            cast = "\n".join(f"- {display(a)} plays {actor}" for a, actor in stage["actors"].items())
            path.write_text(f"# Scenario: {scenario['name']}\n\n{scenario['setup']}\n\n## Cast\n{cast}\n- Game master: {display(gm)}\n",
                            encoding="utf-8")
            await self._emit(Event("turn", "triad", f"{scenario['setup']}\n\n{cast}", {"kind": "scenario", "title": f"🎲 Scenario: {scenario['name']}"}))
            for t in range(1, stage["turns"] + 1):
                await self._emit(Event("log", "triad", f"Scenario '{scenario['name']}' · turn {t}/{stage['turns']}"))
                results = await asyncio.gather(*(self.turn_with_fallback(agent, "think", lambda w, a=agent: prompts.sim_move(
                    w, self.request, self.plan, stage, scenario, stage["actors"][a], state, log, t, stage["turns"],
                    self.history, self.skills)) for agent in stage["actors"]))
                moves = {}
                for agent, res in zip(stage["actors"], results):
                    if res.ok:
                        moves[stage["actors"][agent]] = res.text.strip()
                        await self._emit(Event("turn", res.agent, res.text.strip(),
                                               {"kind": "move", "title": f"{display(res.agent)} as {stage['actors'][agent][:60]} · turn {t}"}))
                gm_res = await self.turn_with_fallback(gm, "think", lambda w: prompts.sim_adjudicate(
                    w, self.request, stage, scenario, state, log, moves, t, stage["turns"]), as_lead=True)
                verdict = parse_json_object(gm_res.text) if gm_res.ok else None
                if verdict is None:
                    verdict = {"narrative": gm_res.text.strip() or "(no adjudication)", "state": state, "ended": False, "outcome": ""}
                narrative, state = str(verdict.get("narrative", "")), str(verdict.get("state") or state)
                outcome = str(verdict.get("outcome") or "")
                await self._emit(Event("turn", gm_res.agent, narrative + (f"\n\n**Outcome:** {outcome}" if outcome else ""),
                                       {"kind": "gm", "title": f"🎲 Game master ({display(gm_res.agent)}) · turn {t}"}))
                entry = f"\n## Turn {t}\n" + "".join(f"\n### {who}\n{text}\n" for who, text in moves.items())
                entry += f"\n### Game master\n{narrative}\n\n**State:** {state}\n" + (f"\n**Outcome:** {outcome}\n" if outcome else "")
                with path.open("a", encoding="utf-8") as f:
                    f.write(entry)
                log += f"Turn {t}: {narrative}\n"
                if verdict.get("ended"):
                    break

    async def turn_with_fallback(self, agent: str, mode: str, prompt_for: Callable[[str], str], *, as_lead: bool = False) -> Result:
        """A single turn; if the agent is out of usage or fails, a teammate takes the same seat."""
        tried: set[str] = set()
        # skip a member whose subscription is already known to be out, instead of failing on it again
        who: str | None = agent if self.member_available(agent) and agent in self.team else self.substitute({agent})
        result = Result(agent, False, error="no teammate available")
        while who is not None:
            if who != agent:
                await self._emit(Event("log", "triad", f"{display(who)} takes {display(agent)}'s seat this turn."))
            result = await self.turn(who, mode, prompt_for(who), as_lead=as_lead)
            if result.ok:
                return result
            tried.add(who)
            who = self.substitute(tried)
        return result

    async def _gate(self, index: int, stage: dict[str, Any], folder: str, areas: list[Area]) -> tuple[str, str]:
        """The lead reviews a stage: may request fixes (research/build), adapts the remaining stages, or halts.
        Returns (summary, unaddressed notes)."""
        cfg = self.config
        fixable = stage["type"] in ("research", "build") and areas
        max_rounds = max(1, cfg.review_rounds) if fixable else 1
        summary = ""
        for r in range(1, max_rounds + 1):
            await self._emit(Event("phase", text=f"Lead review of stage {index}" + (f" (round {r})" if r > 1 else "")))
            verify = await self._verify(self.plan) if stage["type"] == "build" else ""
            files = self._git("ls-files", "--cached", "--others", "--exclude-standard").stdout.strip()
            files = "\n".join(files.splitlines()[:300]) or "(no files)"
            handoffs = {a.label: a.handoff or "(no report)" for a in areas}
            remaining = self.plan["stages"][index:]
            lead, data = await self.lead_call(lambda lead: prompts.gate(
                lead, self.team, self.request, self.plan, stage, index, folder, handoffs, self.board.transcript(30),
                verify, files, r, max_rounds, remaining, self.replans, cfg.max_replans, self.skills, self.staffing_brief()))
            if data is None:
                await self._emit(Event("log", "triad", "The review didn't produce a usable verdict; moving on."))
                return summary or "(no summary)", ""
            summary = str(data.get("summary") or summary)
            feedback = {k: v for k, v in _agent_map(data.get("feedback"), self.team).items() if v} if fixable else {}
            approved = bool(data.get("approved")) or not feedback
            await self._emit(Event("review", lead, "approved" if approved else "changes requested",
                                   {"kind": "review", "round": r, "stage": stage["name"], **data, "feedback": feedback,
                                    "approved": approved}))
            reason_all = str(data.get("halt_all") or "").strip()
            if reason_all and self.replans < cfg.max_replans:
                await self._emit(Event("halt", "all", reason_all, {"scope": "all"}))
                raise Replan(reason_all)
            team, hires = self.propose_staffing(data, self.team)
            if team != self.team:
                await self.commit_team(team, hires, lead)
                if not isinstance(data.get("next_stages"), list):  # re-fit the remaining stages to the new team
                    data["next_stages"] = remaining
            new_stages = data.get("next_stages")
            if isinstance(new_stages, list):
                parsed = parse_stages(new_stages, self.team, self.lead() or self.team[0])
                limit = MAX_STAGES - index
                parsed = parsed[:max(0, limit)]
                if json.dumps(parsed, sort_keys=True) != json.dumps(remaining, sort_keys=True):
                    self.plan["stages"] = self.plan["stages"][:index] + parsed
                    (self.board.dir / "plan.json").write_text(json.dumps(self.plan, indent=2))
                    await self._emit(Event("adapt", lead, "", {"stages": parsed, "from": index + 1}))
            if approved:
                return summary, ""
            notes = "\n".join(f"- {a}: {t}" for a, t in feedback.items())
            if r == max_rounds:
                await self._emit(Event("log", "triad", "Review rounds used up; remaining notes go to final assembly."))
                return summary, notes
            await self._emit(Event("phase", text=f"Fixes requested by the lead ({', '.join(display(a) for a in feedback)})"))
            for agent, text in feedback.items():
                self.board.post("lead", agent, f"Review feedback: {text}", kind="review")
            only = {a.id: feedback[a.worker] for a in areas if a.worker in feedback}
            await self._work_round(areas, lambda area: lambda w, note: prompts.fix(
                w, self.team, lead, self.request, self.plan, stage, area.assignment, only.get(area.id, ""), feedback,
                self.skills, note), kind="fix", only=only)
            self._snapshot(f"triad: stage {index} fixes {r}")
        return summary, ""

    async def _make_plan(self, request: str, proposals: dict[str, str], feedback: str = "",
                         previous: dict[str, Any] | None = None, source: str = "client") -> dict[str, Any]:
        tried: tuple[str, ...] = ()
        while (lead := self.lead(exclude=tried)) is not None:
            prompt = prompts.plan(lead, self.team, request, proposals, feedback, previous, source, self.skills,
                                  self.staffing_brief(), self.prior)
            for attempt in range(2):
                result = await self.turn(lead, "think", prompt, as_lead=True)
                if not result.ok:
                    break
                raw = parse_json_object(result.text, lambda d: isinstance(d.get("stages"), list) or isinstance(d.get("assignments"), list))
                team, hires = self.propose_staffing(raw or {}, self.team)
                plan = parse_plan(result.text, team, lead)
                if plan:
                    plan["team"] = team
                    plan["hires"] = [{"name": n, "provider": p, "reason": r} for n, p, r in hires]
                    await self._emit(Event("plan", lead, "", plan))
                    return plan
                prompt += "\n\nYour previous reply was not a valid JSON object of the required shape. Reply with the JSON only."
            tried += (lead,)
        raise RuntimeError("Nobody on the team could produce a plan (all agents unavailable or failing).")


    # ------------------------------------------------------------------ the client talks to the lead

    def _record(self, e: Event) -> None:
        """Keep a terse line per meaningful event for the reporter."""
        d = e.data if isinstance(e.data, dict) else {}
        line = ""
        if e.kind == "activity" and e.data not in ("thought", "proposal"):
            line = f"{e.agent}: {e.text.strip().splitlines()[0][:200]}" if e.text.strip() else ""
        elif e.kind == "chat":
            line = f"{e.agent} -> {d.get('to', 'all')}: {e.text[:240]}"
        elif e.kind in ("log", "error"):
            line = f"[{e.agent}] {e.text[:240]}"
        elif e.kind == "phase":
            line = f"=== {e.text}"
        elif e.kind == "review":
            line = f"lead {d.get('kind', 'review')} of {d.get('stage', 'work')}: {e.text}. {str(d.get('summary', ''))[:300]}"
        elif e.kind == "turn":
            line = f"{d.get('title', e.agent)}: {e.text.strip()[:300]}"
        elif e.kind in ("halt", "staff", "adapt", "lead_reply"):
            line = f"{e.kind} ({e.agent}): {(e.text or json.dumps(d))[:240]}"
        if line:
            self.feed.append(line)
            if len(self.feed) > 3000:
                drop = len(self.feed) - 2000
                self.feed, self._feed_seen = self.feed[drop:], max(0, self._feed_seen - drop)

    def narrator(self) -> str | None:
        """Who writes progress reports: an idle member, preferring Antigravity, never blocking the lead."""
        want = (self.config.narrator or "auto").strip().lower()
        if want in ("off", "none", "false", ""):
            return None
        ok = [m for m in self.team if self.member_available(m)]
        if want != "auto":
            named = _canon(want, self.team)
            return named if named in ok else None
        lead = self.lead()
        ranked = sorted(ok, key=lambda m: (provider_of(m) != "antigravity", m == lead, self.busy.get(m, 0)))
        return ranked[0] if ranked else None

    async def _narrate(self) -> None:
        interval = max(0.2, float(self.config.narrator_interval))
        while True:
            await asyncio.sleep(interval)
            new = self.feed[self._feed_seen:]
            meaningful = [x for x in new if not x.startswith(("=== Lead review", "[triad] Verification"))]
            if len(meaningful) < 3:
                continue  # nothing worth reporting: no call, no usage
            who = self.narrator()
            if who is None:
                continue
            self._feed_seen = len(self.feed)
            result = await self.turn(who, "think", prompts.narrate(who, self.request, self.plan, self.progress, new),
                                     narrating=True)
            if result.ok and result.text.strip():
                self.progress = result.text.strip()
                await self._emit(Event("progress", who, self.progress))

    def status_text(self) -> str:
        """What everyone is doing right now (local, costs nothing)."""
        lines = []
        for m in self.team:
            area = next((a for a in self.active_areas if a.worker == m and a.running), None)
            state = "working" if self.busy.get(m) else "idle"
            job = f" as {area.assignment.get('role', '')}: {area.assignment.get('task', '')[:160]}" if area else ""
            lines.append(f"- {m}: {state}{job}. Last activity: {self.last_activity.get(m, '(none yet)')}")
        if self.progress:
            lines.append(f"Latest progress report: {self.progress}")
        return "\n".join(lines) or "(no members)"

    def progress_text(self) -> str:
        total = len(self.plan.get("stages", []))
        lines = []
        if self.stage and self.stage_index:
            lines.append(f"Stage {self.stage_index}/{total}: {self.stage['name']} ({self.stage['type']})")
        for h in self.history:
            lines.append(f"✓ Stage {h['index']} {h['name']}: {h['summary'][:200]}")
        return "\n".join(lines + ["", self.status_text()])

    def ask_lead(self, message: str) -> asyncio.Task:
        """The client messages the lead privately; handled in the background so work never blocks."""
        return asyncio.create_task(self._client_desk(message))

    async def _client_desk(self, message: str) -> None:
        async with self._client_lock:
            can_replan = self._stage_task is not None and not self._stage_task.done()
            lead, data = await self.lead_call(lambda lead: prompts.client_desk(
                lead, self.request, self.plan, self.stage if can_replan else None, self.stage_index, self.history,
                self.status_text(), self.board.transcript(20), message, can_replan))
            if data is None:
                await self._emit(Event("lead_reply", lead, "I couldn't respond just now (see the activity log). "
                                       "Try again, or use /status for a quick summary.", {}))
                return
            done: list[str] = []
            requirement = str(data.get("requirement") or "").strip()
            if requirement and self.plan:
                self.plan.setdefault("client_input", []).append(requirement)
                (self.board.dir / "plan.json").write_text(json.dumps(self.plan, indent=2))
                self.save_state()
                done.append(f"recorded as a requirement for everyone: {requirement}")
            for member, note in _agent_map(data.get("notes"), self.team).items():
                self.board.post("lead", member, f"Direction from the client (via lead): {note}", kind="direction")
                done.append(f"told {display(member)}: {note}")
            if _halts(data, self.team) and self.active_areas:
                await self._apply_actions(self.active_areas, {"halt": data.get("halt")}, client=True)
                done += [f"stopped and redirected {display(h['agent'])}" for h in _halts(data, self.team)]
            if isinstance(data.get("next_stages"), list) and can_replan:
                parsed = parse_stages(data["next_stages"], self.team, lead)[:max(0, MAX_STAGES - self.stage_index)]
                self.plan["stages"] = self.plan["stages"][:self.stage_index] + parsed
                (self.board.dir / "plan.json").write_text(json.dumps(self.plan, indent=2))
                await self._emit(Event("adapt", lead, "", {"stages": parsed, "from": self.stage_index + 1}))
                done.append("changed the remaining stages")
            replan = str(data.get("replan") or "").strip()
            if replan and can_replan:
                done.append("stopping the team to rework the plan (you'll approve the new one)")
            await self._emit(Event("lead_reply", lead, str(data.get("reply") or "(no reply)"), {"actions": done}))
            if replan and can_replan:
                await self._pivot_now(f"{message}\n(The lead's reading: {replan})")

    async def _pivot_now(self, reason: str) -> None:
        self._pivot = reason
        await self._emit(Event("halt", "all", reason.splitlines()[0][:300], {"scope": "all", "by": "client"}))
        self.board.post("lead", "all", "STOP: the client changed direction. A revised plan is coming.", kind="direction")
        if self._stage_task and not self._stage_task.done():
            self._stage_task.cancel()

    # ------------------------------------------------------------------ work rounds + supervision

    async def _work_round(self, areas: list[Area], prompt_for: PromptFor, *, kind: str,
                          only: dict[str, str] | None = None) -> None:
        """Run areas in parallel while the lead supervises. `only` limits the round to some areas."""
        active = [a for a in areas if only is None or a.id in only]
        if not active:
            return
        self._replan_reason = ""
        self.active_areas = active
        supervisor = asyncio.create_task(self._supervise(active, kind, len(self.board.all())))
        try:
            await asyncio.gather(*(self._area_loop(a, prompt_for) for a in active))
        finally:
            self.active_areas = []
            supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supervisor
        await self._flush_board()
        if self._replan_reason:
            raise Replan(self._replan_reason)

    async def _area_loop(self, area: Area, prompt_for: PromptFor) -> None:
        make = prompt_for(area)
        while True:
            correction, area.redirect = area.redirect, ""
            area.task = asyncio.create_task(self.area_turn(
                area, lambda w, note: make(w, "\n".join(x for x in (note, correction) if x))))
            area.running = True
            try:
                result = await area.task
            except asyncio.CancelledError:
                me = asyncio.current_task()
                if me is not None and me.cancelling():
                    raise  # the whole project is being stopped
                if self._replan_reason:
                    area.status = "HALTED"
                    return
                if area.redirect:
                    continue  # halted by the lead: restart with its correction
                raise
            finally:
                area.running = False
            if result.ok:
                area.handoff = result.text[-6000:]
                m = re.search(r"STATUS:\s*\**\s*(READY|CHANGED|BLOCKED)", result.text, re.I)
                area.status = m.group(1).upper() if m else "CHANGED"
            else:
                area.status = "FAILED"
            await self._emit(Event("log", area.worker, f"{area.label}: {area.status}"))
            return

    async def _supervise(self, areas: list[Area], kind: str, seen: int) -> None:
        """The lead's desk during a round: event-driven, so it only spends usage when something needs it."""
        cfg = self.config
        want_checkin = kind == "build" and cfg.direction_check and len(self.team) > 1
        checkins: dict[str, str] = {}
        questions: list[dict[str, Any]] = []
        first_question = 0.0
        started = time.time()
        while True:
            await asyncio.sleep(cfg.poll_seconds)
            msgs = self.board.all()
            new, seen = msgs[seen:], len(msgs)
            for m in new:
                if m.get("to") != "lead":
                    continue
                if m.get("kind") == "checkin":
                    checkins[m.get("from", "?")] = m.get("text", "")
                else:
                    questions.append(m)
                    first_question = first_question or time.time()
            running = [a for a in areas if a.running]
            if not running:
                continue
            if want_checkin:
                waiting_on = {a.worker for a in running} - set(checkins)
                timed_out = time.time() - started > cfg.checkin_timeout
                if checkins and (not waiting_on or timed_out):
                    want_checkin = False
                    await self._direction_check(areas, checkins)
                elif timed_out:
                    want_checkin = False
            if questions and time.time() - first_question >= cfg.question_batch_seconds:
                batch, questions, first_question = questions, [], 0.0
                await self._answer(areas, batch)

    async def _direction_check(self, areas: list[Area], checkins: dict[str, str]) -> None:
        cfg = self.config
        await self._emit(Event("log", "triad", f"Everyone has checked in; the lead is checking directions."))
        lead, data = await self.lead_call(lambda lead: prompts.direction(
            lead, self.request, self.plan, self.stage, checkins, self.board.transcript(30), self.replans, cfg.max_replans))
        if data is None:
            return
        verdict = str(data.get("verdict") or "on_track")
        notes = {k: v for k, v in _agent_map(data.get("notes"), self.team).items() if v}
        await self._emit(Event("review", lead, verdict, {"kind": "direction", **data, "notes": notes}))
        for agent, note in notes.items():
            if not any(h.get("agent") == agent for h in _halts(data, self.team)):
                self.board.post("lead", agent, note, kind="direction")
        await self._apply_actions(areas, data)

    async def _answer(self, areas: list[Area], batch: list[dict[str, Any]]) -> None:
        cfg = self.config
        askers = sorted({q.get("from", "") for q in batch})
        if self.lead_answers >= cfg.lead_answer_budget:
            for who in askers:
                self.board.post("lead", who, "I'm out of question budget for this project: make the most sensible "
                                "assumption, note it in your handoff, and continue.", kind="answer")
            return
        self.lead_answers += 1
        await self._emit(Event("log", "triad", f"The lead is answering {len(batch)} question(s) from {', '.join(askers)}."))
        lead, data = await self.lead_call(lambda lead: prompts.answer(
            lead, self.request, self.plan, batch, self.board.transcript(30), self.replans, cfg.max_replans))
        answered: set[str] = set()
        for ans in (data or {}).get("answers") or []:
            if not isinstance(ans, dict) or not ans.get("text"):
                continue
            to = _canon(str(ans.get("to", "")), self.team) or "all"
            self.board.post("lead", to, str(ans["text"]), kind="answer")
            answered |= set(askers) if to == "all" else {to}
        for who in askers:
            if who not in answered:
                self.board.post("lead", who, "No specific answer from the lead: use your best judgment consistent "
                                "with the plan, and note the assumption in your handoff.", kind="answer")
        if data:
            await self._apply_actions(areas, data)

    async def _apply_actions(self, areas: list[Area], data: dict[str, Any], *, client: bool = False) -> None:
        cfg = self.config
        reason_all = str(data.get("halt_all") or "").strip()
        if reason_all:
            if self.replans < cfg.max_replans:
                await self._halt_all(areas, reason_all)
                return
            self.board.post("lead", "all", f"Important correction for everyone: {reason_all}", kind="direction")
        for h in _halts(data, self.team):
            area = next((a for a in areas if a.running and a.worker == h["agent"]), None)
            if area is None:
                continue
            if area.halts >= cfg.max_halts_per_agent and not client:
                self.board.post("lead", h["agent"], f"Course correction: {h['reason']}", kind="direction")
                continue
            area.halts += 1
            area.redirect = (f"THE LEAD STOPPED YOU MID-WORK for a course correction:\n{h['reason']}\n"
                             "What you did so far is still in the workspace: keep what is right, change direction "
                             "as instructed, and continue. Do not check in again unless your approach changed a lot.")
            await self._emit(Event("halt", h["agent"], h["reason"], {"scope": "agent"}))
            if area.task and not area.task.done():
                area.task.cancel()

    async def _halt_all(self, areas: list[Area], reason: str) -> None:
        self._replan_reason = reason
        await self._emit(Event("halt", "all", reason, {"scope": "all"}))
        self.board.post("lead", "all", f"STOP: everyone halt. The plan is being revised: {reason}", kind="direction")
        for a in areas:
            if a.task and not a.task.done():
                a.task.cancel()

    # ------------------------------------------------------------------ board / git / verify

    async def _watch_board(self) -> None:
        while True:
            await self._flush_board()
            await asyncio.sleep(0.7)

    async def _flush_board(self) -> None:
        for msg in self.board.poll():
            await self._emit(Event("chat", msg.get("from", ""), msg.get("text", ""), msg))

    def _git(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.workspace, capture_output=True, text=True, check=check)

    def _setup_git(self) -> str:
        """Make the workspace a git repo so every phase is a snapshot you can diff or roll back."""
        self._branch_pending = False
        if self._git("rev-parse", "--is-inside-work-tree").returncode != 0:
            self._git("init", "-q")
            fresh = True
        else:
            fresh = False
        top = Path(self._git("rev-parse", "--show-toplevel").stdout.strip() or self.workspace)
        exclude = top / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        current = exclude.read_text() if exclude.exists() else ""
        missing = [p for p in DEFAULT_EXCLUDES if p not in current.splitlines()]
        if missing:  # keep caches and dependency folders out of snapshots, without editing the project's .gitignore
            with exclude.open("a") as f:
                f.write("\n" + "\n".join(missing) + "\n")
        dirty = bool(self._git("status", "--porcelain").stdout.strip())
        has_head = self._git("rev-parse", "HEAD").returncode == 0
        if fresh or not has_head:
            self._snapshots = True
            if dirty:
                self._commit("triad: starting point")
        elif dirty:
            self._snapshots = False
            return "Git has uncommitted changes here, so Triad won't make snapshot commits this run."
        else:
            self._snapshots = True
            self._branch_pending = True
        return ""

    def _branch(self, name: str) -> str:
        if not self._branch_pending:
            return ""
        self._branch_pending = False
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "project"
        branch = f"triad/{slug}-{time.strftime('%m%d-%H%M')}"
        if self._git("switch", "-c", branch).returncode == 0:
            return f"Working on new git branch {branch} (your original branch is untouched)."
        return ""

    def _commit(self, message: str) -> None:
        self._git("add", "-A")
        ident = [] if self._git("config", "user.email").stdout.strip() else ["-c", "user.name=Triad", "-c", "user.email=triad@localhost"]
        self._git(*ident, "commit", "-q", "-m", message)

    def _snapshot(self, message: str) -> None:
        if self._snapshots and self._git("status", "--porcelain").stdout.strip():
            self._commit(message)

    async def _verify(self, plan: dict[str, Any]) -> str:
        cmd = (plan.get("verify") or "").strip()
        if not cmd:
            return ""
        await self._emit(Event("activity", "triad", f"$ {cmd}", "tool"))
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, cwd=str(self.workspace), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True)
            out, _ = await asyncio.wait_for(proc.communicate(), self.config.verify_timeout)
            text = out.decode("utf-8", "replace")[-4000:]
            verdict = "passed" if proc.returncode == 0 else f"failed (exit {proc.returncode})"
        except asyncio.TimeoutError:
            proc.kill()
            text, verdict = "", f"timed out after {self.config.verify_timeout}s"
        await self._emit(Event("log", "triad", f"Verification {verdict}."))
        return f"$ {cmd}\n{verdict}\n{text}"


ALIASES = {"gemini": "antigravity", "agy": "antigravity", "claude code": "claude", "openai": "codex", "gpt": "codex"}


def parse_json_object(text: str, accept: Callable[[dict[str, Any]], bool] = lambda d: True) -> dict[str, Any] | None:
    """The first JSON object in a model reply that `accept`s (tolerates prose and code fences)."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            candidate, _ = decoder.raw_decode(text, start)
            if isinstance(candidate, dict) and accept(candidate):
                return candidate
        except ValueError:
            pass
        start = text.find("{", start + 1)
    return None


def _canon(name: str, team: list[str]) -> str:
    """Map whatever the lead called someone ("Codex", "Claude #2", "gemini", "all") to a member name, or ""."""
    n = re.sub(r"\s*#\s*|\s+", "-", name.lower().strip())
    if n in ("all", "everyone", "team"):
        return "all"
    base, _, num = n.rpartition("-") if re.search(r"-\d+$", n) else (n, "", "")
    base = ALIASES.get(base, base)
    n = f"{base}-{num}" if num and num != "1" else base
    if n in team:
        return n
    if not num:  # "codex" when the only codex member is codex-2
        same = [m for m in team if provider_of(m) == base]
        if len(same) == 1:
            return same[0]
    return ""


def _agent_map(raw: Any, team: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            who = _canon(str(k), team)
            if who and who != "all" and str(v or "").strip():
                out[who] = str(v).strip()
    return out


def _halts(data: dict[str, Any], team: list[str]) -> list[dict[str, str]]:
    out = []
    for h in data.get("halt") or []:
        if isinstance(h, dict):
            who = _canon(str(h.get("agent", "")), team)
            if who and who != "all":
                out.append({"agent": who, "reason": str(h.get("reason") or "change direction as discussed")})
    return out


MAX_STAGES = 6
STAGE_ALIASES = {"collect": "research", "data": "research", "data collection": "research", "investigate": "research",
                 "discussion": "debate", "discuss": "debate", "analysis": "debate", "simulation": "simulate",
                 "wargame": "simulate", "roleplay": "simulate", "role-play": "simulate", "write": "build",
                 "writing": "build", "implement": "build", "code": "build", "development": "build"}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40] or "stage"


def stage_folder(index: int, stage: dict[str, Any]) -> str:
    return f"work/{index:02d}-{_slug(stage['name'])}"


def _as_list(value: Any) -> list[str]:
    return [str(x) for x in value] if isinstance(value, list) else [str(value)] if value else []


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _assignments(raw_list: Any, team: list[str]) -> list[dict[str, Any]]:
    raw = [a for a in (raw_list or []) if isinstance(a, dict) and a.get("task")]
    for a in raw:
        a["owns"], a["deliverables"] = _as_list(a.get("owns")), _as_list(a.get("deliverables"))
    by_agent: dict[str, dict[str, Any]] = {}
    leftovers = []
    for a in raw:
        name = _canon(str(a.get("agent", "")), team)
        if name and name != "all" and name not in by_agent:
            by_agent[name] = a
        else:
            leftovers.append(a)
    for name in team:  # anyone without work gets the next unclaimed assignment
        if name not in by_agent and leftovers:
            by_agent[name] = leftovers.pop(0)
    for extra in leftovers:  # more assignments than people: fold into whoever has least listed work
        target = min(by_agent.values(), key=lambda x: len(str(x.get("task", ""))))
        target["task"] = f"{target['task']}\n\nAlso: {extra.get('task')}"
        target["owns"] = target["owns"] + extra["owns"]
    return [{"agent": n, "role": str(by_agent[n].get("role") or "Team member"), "task": str(by_agent[n]["task"]),
             "owns": by_agent[n]["owns"], "deliverables": by_agent[n]["deliverables"]} for n in team if n in by_agent]


def parse_stage(raw: Any, team: list[str], lead: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type") or "build").lower().strip()
    kind = STAGE_ALIASES.get(kind, kind)
    if kind not in prompts.STAGE_TYPES:
        kind = "build"
    stage: dict[str, Any] = {"name": str(raw.get("name") or kind.title()), "type": kind,
                             "goal": str(raw.get("goal") or ""), "skills": _as_list(raw.get("skills"))}
    if kind in ("research", "build"):
        stage["assignments"] = _assignments(raw.get("assignments"), team)
        return stage if stage["assignments"] else None
    if kind == "debate":
        positions = _agent_map(raw.get("positions"), team)
        defaults = ["the most likely outcome (base case)", "the strongest alternative to the base case",
                    "devil's advocate: what everyone is missing"]
        for i, a in enumerate(team):
            positions.setdefault(a, defaults[i % 3])
        stage.update(topic=str(raw.get("topic") or raw.get("goal") or stage["name"]),
                     positions={a: positions[a] for a in team}, rounds=_clamp(raw.get("rounds"), 1, 3, 2))
        return stage
    actors = _agent_map(raw.get("actors"), team)
    scenarios = []
    for sc in (raw.get("scenarios") or [])[:3]:
        if isinstance(sc, dict) and (sc.get("setup") or sc.get("name")):
            scenarios.append({"name": str(sc.get("name") or f"Scenario {len(scenarios) + 1}"),
                              "setup": str(sc.get("setup") or sc.get("name"))})
        elif isinstance(sc, str) and sc.strip():
            scenarios.append({"name": f"Scenario {len(scenarios) + 1}", "setup": sc})
    if not actors or not scenarios:
        return None
    gm = _canon(str(raw.get("game_master") or ""), team)
    stage.update(actors=actors, scenarios=scenarios, turns=_clamp(raw.get("turns"), 1, 4, 3),
                 game_master=gm if gm and gm != "all" else lead)
    return stage


def parse_stages(raw_list: Any, team: list[str], lead: str) -> list[dict[str, Any]]:
    out = []
    for raw in raw_list or []:
        stage = parse_stage(raw, team, lead)
        if stage:
            out.append(stage)
    return out[:MAX_STAGES]


def parse_plan(text: str, team: list[str], lead: str = "") -> dict[str, Any] | None:
    """Pull the JSON plan out of a model reply and make it fit the team exactly."""
    plan = parse_json_object(text, lambda d: isinstance(d.get("stages"), list) or isinstance(d.get("assignments"), list))
    if plan is None:
        return None
    lead = lead or (team[0] if team else "")
    raw_stages = plan.get("stages")
    if not isinstance(raw_stages, list):  # a single-stage plan written the old way
        raw_stages = [{"name": "Build", "type": "build", "goal": plan.get("summary", ""), "assignments": plan.get("assignments")}]
    stages = parse_stages(raw_stages, team, lead)
    if not stages:
        return None
    for i, stage in enumerate(stages, 1):  # research notes default to the stage folder
        if stage["type"] == "research":
            for a in stage["assignments"]:
                if not a["owns"]:
                    a["owns"] = [f"{stage_folder(i, stage)}/{a['agent']}.md"]
    arch = plan.get("architecture") or ""
    questions = [str(q).strip() for q in (plan.get("questions") or []) if str(q).strip()] if isinstance(plan.get("questions"), list) else []
    result = {"project_name": str(plan.get("project_name") or "project"), "summary": str(plan.get("summary") or ""),
            "deliverable": str(plan.get("deliverable") or ""),
            "architecture": arch if isinstance(arch, str) else json.dumps(arch, indent=2),
            "stages": stages, "verify": str(plan.get("verify") or ""), "questions": questions[:6]}
    result["client_brief"] = str(plan.get("client_brief") or "").strip() or default_brief(result)
    return result


def default_brief(plan: dict[str, Any]) -> str:
    """A plain summary for when the lead didn't write one."""
    icons = {"research": "🔎", "build": "🛠", "debate": "⚖", "simulate": "🎲"}
    lines = [plan["summary"] or plan["project_name"], ""]
    if plan.get("deliverable"):
        lines += [f"**You'll get:** {plan['deliverable']}", ""]
    lines.append("**How we'll work:**")
    for i, st in enumerate(plan["stages"], 1):
        who = st.get("assignments") and ", ".join(display(a["agent"]) for a in st["assignments"]) or ""
        lines.append(f"{i}. {icons.get(st['type'], '')} {st['name']}" + (f" ({who})" if who else ""))
    return "\n".join(lines)
