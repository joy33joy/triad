"""The interactive terminal app: type a project, watch three AIs build it."""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Input, RichLog, Static

from .agents import AGENTS, COLORS, DISPLAY, color, display, installed, name_members, provider_of
from .company import Cancelled, Company, Event
from .config import Config

SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

HELP = """[b]Start a project[/b] by just typing what you want built, e.g.
  [i]Build a pomodoro timer web app with stats and a dark mode[/i]

While the team works, what you type goes [b]only to the lead[/b]: ask how it's going, ask questions, or
change direction. The lead answers you and steers the team (notes, restarts, new stages, or a new plan).
[b]Commands[/b]
  /status             instant progress summary (free: no AI call)
  /resume             continue an unfinished project in this folder (also: Enter on an empty prompt)
  /all <msg>          post to the whole team chat;  /to <member> <msg>  message one member directly
  /stop               stop the current project (work so far stays in the folder)
  /lead <agent>       who plans and does final assembly (now: {lead})
  /rounds <n>         peer integration rounds (now: {rounds})
  /reviews <n>        max lead review -> fix cycles (now: {reviews})
  /brainstorm on|off  everyone proposes ideas before planning (now: {brainstorm})
  /details            during plan review: the full plan and everyone's proposals
  /team [spec]        show or set the team, e.g. /team claude,codex,claude (any mix, any size)
  /view [summary|detail]  clean progress view, or everything (also ctrl+o)
  /clear              clear the screen       /quit   exit (also Ctrl+C)
Everything happens in: [b]{workspace}[/b] (a git repo; each phase is a commit)."""


class AgentCard(Static):
    def __init__(self, agent: str) -> None:
        super().__init__(id=f"card-{agent}", classes="card")
        self.agent = agent
        self.state = "idle"
        self.last = ""
        self.halted_at = 0.0

    def refresh_card(self, company: Company | None, tick: int) -> None:
        tint = color(self.agent)
        t = Text()
        t.append(display(self.agent), style=f"bold {tint}")
        provider = provider_of(self.agent)
        sub = company.usage[provider] if company else None              # the subscription (shared by same-provider members)
        mine = company.member_usage.get(self.agent) if company else None  # this member's own work
        if sub and not sub.available():
            until = time.strftime("%H:%M", time.localtime(sub.exhausted_until)) if sub.exhausted_until else "?"
            t.append(f"  ⛔ {sub.exhausted_reason} (until {until})", style="red")
        elif self.state == "halted" or time.time() - self.halted_at < 4:
            t.append("  ✋ halted by lead", style="bold red")
        elif self.state == "benched":
            t.append("  💤 benched", style="dim")
        elif self.state == "reviewing":
            t.append(f"  {SPIN[tick % len(SPIN)]} leading (review)", style=f"bold {tint}")
        elif self.state == "narrating":
            t.append(f"  {SPIN[tick % len(SPIN)]} writing a progress report", style=tint)
        elif self.state in ("working", "thinking"):
            t.append(f"  {SPIN[tick % len(SPIN)]} {self.state}", style=tint)
        else:
            t.append("  ● idle", style="dim")
        t.append("\n")
        t.append((self.last or "—")[:90], style="dim")
        t.append("\n")
        if mine:
            stats = f"{mine.calls} calls · {mine.tokens:,} tok"
            if sub and sub.quota:
                stats += " · " + " ".join(f"{k} {v:.0%}" for k, v in sub.quota.items())
            t.append(stats, style="dim italic")
        self.update(t)


class TriadApp(App):
    TITLE = "Triad"
    CSS = """
    Screen { layout: vertical; }
    #cards { height: 5; }
    .card { width: 1fr; border: round $panel; padding: 0 1; height: 5; }
    #main { height: 1fr; }
    #summary { width: 1fr; border: round $panel; border-title-color: $text-muted; }
    #log { width: 2fr; border: round $panel; border-title-color: $text-muted; }
    #chat { width: 1fr; border: round $panel; border-title-color: $text-muted; }
    #hint { height: 1; color: $text-muted; padding: 0 1; }
    #prompt { border: round $accent; }
    """
    BINDINGS = [("ctrl+c", "quit", "Quit"), Binding("ctrl+o", "toggle_view", "Summary/detail", priority=True),
                ("ctrl+l", "clear", "Clear")]

    def __init__(self, workspace: Path, config: Config, demo: bool = False, initial: str = "",
                 team_spec: list[str] | None = None) -> None:
        super().__init__()
        self.workspace, self.config, self.demo, self.initial = workspace, config, demo, initial
        self.team_spec = team_spec
        self.company: Company | None = None
        self.mode = "idle"  # idle | review | running
        self.plan_future: asyncio.Future | None = None
        self.last_plan: dict[str, Any] | None = None
        self.last_plan_lead = ""
        self.proposals: dict[str, str] = {}
        self.tick = 0

    # ---------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        with Horizontal(id="cards"):
            for a in self.roster():
                yield AgentCard(a)
        with Horizontal(id="main"):
            yield RichLog(id="summary", wrap=True, markup=False, highlight=False, min_width=20)
            yield RichLog(id="log", wrap=True, markup=False, highlight=False, min_width=20)
            yield RichLog(id="chat", wrap=True, markup=False, highlight=False, min_width=20)
        yield Static(id="hint")
        yield Input(id="prompt", placeholder="What should the team build?")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#log", RichLog).border_title = "Activity (detail)"
        self.query_one("#summary", RichLog).border_title = "Progress  ·  ctrl+o for the detailed log"
        self.set_view(self.config.view)
        self.query_one("#chat", RichLog).border_title = "Team chat"
        self.sub_title = f"{self.workspace}" + ("  [demo: offline fake agents]" if self.demo else "")
        team = [display(a) for a in self.roster()]
        self._write(Text.from_markup(
            "[b]Welcome to Triad[/b]: a small AI company that takes on software, research, analysis and more.\n"
            f"Team on duty: [b]{', '.join(team) or 'nobody (run `triad doctor`)'}[/b]. Lead: [b]{display(self.config.lead)}[/b].\n"
            "Describe the project and press Enter. [b]/team[/b] changes who's on the team; [b]/help[/b] for commands.\n"))
        self.set_interval(0.15, self._refresh_cards)
        self._set_mode("idle")
        self.query_one("#prompt", Input).focus()
        self.saved = Company.load_state(self.workspace)
        if Company.resumable(self.saved) and not self.initial:
            st = self.saved
            done = len(st.get("history", []))
            stages = st["plan"]["stages"]
            nxt = stages[done]["name"] if done < len(stages) else "final assembly"
            body = Text()
            body.append(f"{st['plan'].get('project_name', 'project')}", style="bold")
            body.append(f": {st['request'][:200]}\n")
            for h in st.get("history", []):
                body.append(f"✓ {h['name']}\n", style="green")
            body.append(f"→ next: stage {done + 1}/{len(stages)}, {nxt}\n" if done < len(stages) else "→ next: final assembly\n")
            body.append("\nPress Enter (or /resume) to continue where it stopped, or type a new request to start fresh "
                        "(the unfinished one is archived in .triad/history).", style="dim")
            self._write(Panel(body, title="⏸ Unfinished project in this folder", border_style="yellow", title_align="left"))
            self.query_one("#prompt", Input).placeholder = "Enter = resume · or type a new request"
        elif self.saved and self.saved.get("status") == "done" and not self.initial:
            name = self.saved.get("plan", {}).get("project_name", "a project")
            self._write(Text(f"Last project here: {name} (delivered). A new request will build on it.", style="dim"))
        if self.initial:
            self._start(self.initial)

    def _new_company(self) -> Company:
        return Company(self.workspace, self.config, self.handle_event, self.ask_plan, demo=self.demo, team_spec=self.team_spec)

    def roster(self) -> list[str]:
        """Members of the current project, or who would be on the next one."""
        if self.company and self.mode != "idle":
            return list(self.company.team)
        return self._new_company().team

    def _card(self, name: str) -> AgentCard | None:
        for card in self.query(AgentCard):
            if card.agent == name:
                return card
        return None

    def sync_cards(self, team: list[str], benched: list[str] = ()) -> None:
        """One card per member; benched members keep a dimmed card until the next project."""
        row = self.query_one("#cards", Horizontal)
        keep = list(team) + [b for b in benched if self._card(b)]
        for card in list(self.query(AgentCard)):
            if card.agent not in keep:
                card.remove()
        for name in team:
            if not self._card(name):
                row.mount(AgentCard(name))
        for name in benched:
            if (card := self._card(name)):
                card.state = "benched"

    def _refresh_cards(self) -> None:
        self.tick += 1
        for card in self.query(AgentCard):
            card.refresh_card(self.company, self.tick)

    def _set_mode(self, mode: str) -> None:
        self.mode = mode
        prompt, hint = self.query_one("#prompt", Input), self.query_one("#hint", Static)
        if mode == "idle":
            prompt.placeholder = "What should the team build?"
            hint.update("Enter to start a project · /help for commands")
        elif mode == "review":
            asks = bool(self.last_plan and self.last_plan.get("questions"))
            prompt.placeholder = ("Type your answers (and any changes), or press Enter to let the team decide"
                                  if asks else "Press Enter to start, or type what to change")
            hint.update("Enter = start · type answers or changes and the lead revises · /details = full plan · /cancel")
        else:
            prompt.placeholder = "Talk to the lead: ask about progress, or change direction"
            hint.update("Messages go to the lead only · /status = quick summary · /all <msg> = whole team · /stop")

    # ---------------------------------------------------------------- input

    @on(Input.Submitted, "#prompt")
    async def submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if text.startswith("/"):
            await self._command(text)
            return
        if self.mode == "review" and self.plan_future and not self.plan_future.done():
            self.plan_future.set_result(("approve", "") if text.lower() in ("", "y", "yes", "ok", "go") else ("revise", text))
            self._write(Text(f"› {text or 'approved'}", style="bold"))
            return
        if not text:
            if self.mode == "idle" and Company.resumable(getattr(self, "saved", None)):
                self._start("", resume=self.saved)
            return
        if self.mode == "running" and self.company:
            line = Text("› You → Lead: ", style=f"bold {color('user')}")
            line.append(text)
            self._write(line)
            self.company.ask_lead(text)
            return
        self._start(text)

    async def _command(self, text: str) -> None:
        cmd, _, arg = text[1:].partition(" ")
        arg = arg.strip()
        if cmd in ("quit", "exit", "q"):
            self.exit()
        elif cmd == "help":
            self._write(Text.from_markup(HELP.format(lead=self.config.lead, rounds=self.config.sync_rounds, reviews=self.config.review_rounds,
                                                     brainstorm="on" if self.config.brainstorm else "off",
                                                     workspace=self.workspace)))
        elif cmd == "details":
            if not self.last_plan:
                self._write(Text("No plan yet.", style="dim"))
                return
            for agent, text in self.proposals.items():
                self._write(Panel(Markdown(text.replace("<", "\\<")), title=f"{display(agent)}'s approach",
                                  border_style=color(agent), title_align="left"))
            self._write(self._plan_panel(self.last_plan, self.last_plan_lead))
        elif cmd == "resume":
            if self.mode == "idle" and Company.resumable(Company.load_state(self.workspace)):
                self._start("", resume=Company.load_state(self.workspace))
            else:
                self._write(Text("Nothing to resume here.", style="dim"))
        elif cmd == "view":
            self.set_view(arg if arg in ("summary", "detail") else ("detail" if self.view == "summary" else "summary"))
            self.config.view = self.view
            self.config.save()
        elif cmd == "clear":
            self.action_clear()
        elif cmd in ("stop", "cancel"):
            if self.plan_future and not self.plan_future.done():
                self.plan_future.set_result(("cancel", ""))
            else:
                self.workers.cancel_group(self, "project")
                self._write(Text("Stopping… (work done so far stays in the folder)", style="yellow"))
        elif cmd == "status":
            if self.company and self.mode == "running":
                body = self.company.progress_text()
                if self.company.progress:
                    body = f"Latest report: {self.company.progress}\n\n{body}"
                self._write(Panel(Text(body), title="📊 Status", border_style=color("triad"), title_align="left"))
            else:
                self._write(Text("Nothing is running.", style="dim"))
        elif cmd == "all" and self.company and self.mode == "running" and arg:
            await self.company.say(arg)
        elif cmd == "to" and self.company and self.mode == "running":
            who, _, msg = arg.partition(" ")
            if who in self.company.team and msg:
                await self.company.say(msg, to=who)
            else:
                self._write(Text(f"usage: /to {'|'.join(self.company.team)} <message>", style="red"))
        elif cmd == "lead" and (arg in AGENTS or provider_of(arg) in AGENTS):
            self.config.lead = arg
            self.config.save()
            self._write(Text(f"Lead is now {display(arg)}.", style="green"))
        elif cmd == "rounds" and arg.isdigit():
            self.config.sync_rounds = int(arg)
            self.config.save()
            self._write(Text(f"Max integration rounds: {arg}.", style="green"))
        elif cmd == "reviews" and arg.isdigit():
            self.config.review_rounds = int(arg)
            self.config.save()
            self._write(Text(f"Max lead review cycles: {arg}.", style="green"))
        elif cmd == "brainstorm" and arg in ("on", "off"):
            self.config.brainstorm = arg == "on"
            self.config.save()
            self._write(Text(f"Brainstorm {arg}.", style="green"))
        elif cmd == "team":
            if arg and self.mode != "idle":
                self._write(Text("Change the team between projects (the lead can hire/bench during one).", style="red"))
            elif arg:
                spec = [x.strip() for x in re.split(r"[,\s]+", arg) if x.strip()]
                if arg in ("default", "reset"):
                    spec = []
                if spec and not name_members(spec):
                    self._write(Text("usage: /team claude,codex,claude   (providers: claude, codex, antigravity; add :model to pin one)", style="red"))
                    return
                self.team_spec = None
                self.config.team = spec
                self.config.save()
                self.sync_cards(self.roster())
                self._write(Text(f"Team saved: {', '.join(display(m) for m in self.roster()) or 'nobody'}"
                                 f" (the lead may hire up to {self.config.max_team}).", style="green"))
            else:
                c = self._new_company()
                for m in c.team:
                    model = c.members[m]["model"] or self.config.agents[provider_of(m)].model or "CLI default model"
                    self._write(Text(f"  {display(m)} ({m}): {model}", style=color(m)))
                self._write(Text("Change with /team claude,codex,claude  ·  /team default  ·  pin a model: codex:gpt-6-astra", style="dim"))
        else:
            self._write(Text(f"Unknown or incomplete command: {text}. Try /help.", style="red"))

    def action_clear(self) -> None:
        self.query_one("#log", RichLog).clear()
        self.query_one("#summary", RichLog).clear()

    # ---------------------------------------------------------------- running a project

    def _start(self, request: str, resume: dict[str, Any] | None = None) -> None:
        if self.mode != "idle":
            return
        if resume:
            self._write(Panel(Text(resume["request"][:400]), title=f"▶ Resuming {resume['plan'].get('project_name', 'project')}",
                              border_style=color("user"), title_align="left"))
        else:
            self._write(Panel(Text(request), title="New project", border_style=color("user")))
        self.saved = None
        self.company = self._new_company()
        self.proposals, self.last_plan = {}, (resume or {}).get("plan")
        self._set_mode("running")
        self.run_project(request, resume)

    @work(exclusive=True, group="project")
    async def run_project(self, request: str, resume: dict[str, Any] | None = None) -> None:
        try:
            run = self.company.run(request, resume=resume)
            self.call_after_refresh(lambda: self.sync_cards(self.company.team))
            await run
        except Cancelled:
            self._write(Text("Plan dropped. Describe another project whenever you like.", style="yellow"))
        except asyncio.CancelledError:
            self._write(Text("Stopped.", style="yellow"))
            raise
        except Exception as exc:  # surface, don't crash the app
            self._write(Text(f"Error: {exc}", style="bold red"))
        finally:
            for card in self.query(AgentCard):
                card.state = "idle"
            self._set_mode("idle")

    async def ask_plan(self, plan: dict[str, Any]) -> tuple[str, str]:
        self.plan_future = asyncio.get_running_loop().create_future()
        self._set_mode("review")
        try:
            return await self.plan_future
        finally:
            self._set_mode("running")

    def _write(self, renderable: Any) -> None:
        """Command output and conversation: shown in both views."""
        self.query_one("#log", RichLog).write(renderable)
        self.query_one("#summary", RichLog).write(renderable)

    def set_view(self, view: str) -> None:
        self.view = "detail" if view == "detail" else "summary"
        summary = self.view == "summary"
        self.query_one("#summary").display = summary
        self.query_one("#log").display = not summary
        self.query_one("#chat").display = not summary

    def action_toggle_view(self) -> None:
        self.set_view("detail" if self.view == "summary" else "summary")
        self.config.view = self.view
        self.config.save()

    def _summarize(self, e: Event) -> None:
        """The summary view: progress reports, the conversation with the lead, and milestones only."""
        out = self.query_one("#summary", RichLog)
        d = e.data if isinstance(e.data, dict) else {}
        name = display(e.agent)
        if e.kind == "phase" and e.text.startswith(("Stage ", "Planning", "Brainstorm", "Final assembly", "Changing direction",
                                                     "Replanning", "Revising plan")):
            out.write(Text(f"\n━━ {e.text} ━━", style=f"bold {color('triad')}"))
        elif e.kind == "plan":
            out.write(self._brief_panel(e.data, e.agent))
        elif e.kind == "activity" and e.data == "proposal":
            out.write(Text(f"💡 {name} shared an approach (/details to read it)", style="dim"))
        elif e.kind == "progress":
            out.write(Text.assemble(("📰 ", ""), (time.strftime("%H:%M") + "  ", "dim"), (e.text.strip(), "")))
        elif e.kind == "lead_reply":
            out.write(self._reply_panel(e))
        elif e.kind == "review":
            if d.get("kind") == "direction":
                if e.text != "on_track":
                    who = ", ".join(display(a) for a in d.get("notes") or {}) or "the team"
                    out.write(Text(f"🧭 The lead corrected course for {who}.", style=color("lead")))
            else:
                fixes = ", ".join(display(a) for a in d.get("feedback") or {})
                verdict = "approved ✅" if d.get("approved") else f"asked {fixes} for fixes"
                out.write(Text(f"🔍 Lead review of {d.get('stage', 'the work')}: {verdict}", style=color("lead")))
        elif e.kind == "halt":
            who = "the whole team" if e.agent == "all" else name
            out.write(Text(f"✋ {who} stopped: {e.text[:200]}", style="red"))
        elif e.kind == "staff":
            changes = [f"hired {display(m)}" for m in d.get("added", [])] + [f"benched {display(m)}" for m in d.get("benched", [])]
            out.write(Text(f"👥 The lead {', '.join(changes)}.", style="yellow"))
        elif e.kind == "adapt":
            names = " → ".join(st["name"] for st in d.get("stages", [])) or "no more stages"
            out.write(Text(f"🔀 The lead changed what's next: {names}", style="yellow"))
        elif e.kind == "error" and ("usage limit" in e.text or "not signed in" in e.text):
            out.write(Text(f"⛔ {name} is out of usage; teammates take over its work.", style="red"))
        elif e.kind == "done":
            out.write(Panel(Markdown(e.text), title="✅ Delivered", border_style="green", title_align="left"))
            out.write(self._usage_table())

    def _reply_panel(self, e: Event) -> Panel:
        from rich.console import Group
        parts: list[Any] = [Markdown(e.text.replace("<", "\\<"))]
        actions = (e.data or {}).get("actions") or []
        if actions:
            parts.append(Text("\n" + "\n".join(f"→ {a}" for a in actions), style="dim"))
        return Panel(Group(*parts), title=f"💬 Lead ({display(e.agent)}) → You", border_style=color("lead"), title_align="left")

    def handle_event(self, e: Event) -> None:
        self._summarize(e)
        tint = color(e.agent)
        name = display(e.agent)
        log, chat = self.query_one("#log", RichLog), self.query_one("#chat", RichLog)
        if e.kind == "phase":
            log.write(Text(f"\n━━ {e.text} ━━", style=f"bold {color('triad')}"))
        elif e.kind == "status":
            if (card := self._card(e.agent)):
                card.state = e.text
        elif e.kind == "activity":
            if (card := self._card(e.agent)):
                card.last = ("💡 shared an approach" if e.data == "proposal" else
                             e.text.strip().splitlines()[0] if e.text.strip() else "")
            if e.data == "proposal":
                self.proposals[e.agent] = e.text
                line = Text(f"{name:>11} │ ", style=tint)
                line.append("💡 shared an approach (/details to read it)", style="dim")
                log.write(line)
            elif e.data == "thought":
                pass  # internal reasoning of the lead / thinkers: shown on the card, kept out of the feed
            elif e.data == "text":
                lines = [x for x in e.text.strip().splitlines() if x.strip()]
                line = Text(f"{name:>11} │ ", style=tint)
                line.append(lines[0][:240] if lines else "")
                if len(lines) > 1 or (lines and len(lines[0]) > 240):
                    line.append(" …", style="dim")
                log.write(line)
            else:
                line = Text(f"{name:>11} │ ", style=tint)
                line.append(e.text, style="dim")
                log.write(line)
        elif e.kind == "chat":
            msg = e.data if isinstance(e.data, dict) else {}
            to, kind = msg.get("to", "all"), msg.get("kind", "chat")
            who = f"Lead ({display(self.company.lead() or '')})" if e.agent == "lead" and self.company else name
            head = Text(who, style=f"bold {tint}")
            head.append(f" → {'Lead' if to == 'lead' else display(to)}", style="dim")
            tag = {"question": " ❓ question", "checkin": " 🧭 check-in", "answer": " ↩ answer",
                   "direction": " 🧭 direction", "review": " 🔍 review"}.get(kind, "")
            if tag:
                head.append(tag, style="italic dim")
            chat.write(head)
            chat.write(Text(e.text + "\n"))
        elif e.kind == "review":
            log.write(self._review_panel(e))
        elif e.kind == "turn":
            d = e.data or {}
            border = {"gm": color("lead"), "scenario": color("triad")}.get(d.get("kind"), tint)
            log.write(Panel(Markdown(e.text.replace("<", "\\<")), title=d.get("title", name), border_style=border, title_align="left"))
            if (card := self._card(e.agent)):
                card.last = d.get("title", "")
        elif e.kind == "adapt":
            d = e.data or {}
            body = self._stages_table(d.get("stages", []), d.get("from", 1)) if d.get("stages") else Text("No more stages; final assembly next.")
            log.write(Panel(body, title=f"🔀 {display(e.agent)} changed the remaining workflow",
                            border_style="yellow", title_align="left"))
        elif e.kind == "halt":
            if e.agent == "all":
                for card in self.query(AgentCard):
                    card.state, card.halted_at = "halted", time.time()
                title = ("✋ Changing direction at your request: the team stopped" if (e.data or {}).get("by") == "client"
                         else "✋ Lead halted the whole team: replanning")
                log.write(Panel(Text(e.text), title=title, border_style="red", title_align="left"))
            else:
                if (card := self._card(e.agent)):
                    card.state, card.halted_at = "halted", time.time()
                log.write(Panel(Text(e.text), title=f"✋ Lead halted {name}: restarting with a correction",
                                border_style="red", title_align="left"))
        elif e.kind == "lead_reply":
            log.write(self._reply_panel(e))
        elif e.kind == "progress":
            log.write(Text(f"   progress │ 📰 {e.text.strip()}", style="dim italic"))
        elif e.kind == "staff":
            d = e.data or {}
            lines = Text()
            for m in d.get("added", []):
                lines.append(f"+ hired {display(m)}", style=f"bold {color(m)}")
                lines.append(f"  {d.get('reasons', {}).get(m, '')}\n")
            for m in d.get("benched", []):
                lines.append(f"− benched {display(m)}\n", style="dim")
            log.write(Panel(lines, title=f"👥 {display(e.agent)} changed the team: {', '.join(display(m) for m in d.get('team', []))}",
                            border_style="yellow", title_align="left"))
            self.sync_cards(d.get("team", []), d.get("benched", []))
        elif e.kind == "log":
            log.write(Text(f"{'triad':>11} │ {e.text}", style=color("triad")))
        elif e.kind == "error":
            log.write(Text(f"{'triad':>11} │ {e.text}", style="red"))
        elif e.kind == "plan":
            self.last_plan, self.last_plan_lead = e.data, e.agent
            log.write(self._brief_panel(e.data, e.agent))
        elif e.kind == "done":
            log.write(Panel(Markdown(e.text), title="✅ Delivered", border_style="green", title_align="left"))
            log.write(self._usage_table())

    def _review_panel(self, e: Event) -> Panel:
        d = e.data or {}
        lead = display(e.agent)
        body = Text()
        if d.get("kind") == "direction":
            title = f"🧭 Direction check by {lead}: {e.text.replace('_', ' ')}"
            items = d.get("notes") or {}
            good = e.text == "on_track"
        else:
            rnd = f" (round {d['round']})" if d.get("round", 1) > 1 else ""
            title = f"🔍 {lead} reviewed {d.get('stage', 'the work')}{rnd}: {'✅ approved' if d.get('approved') else 'changes requested'}"
            if d.get("summary"):
                body.append(str(d["summary"]) + "\n")
            items = d.get("feedback") or {}
            good = bool(d.get("approved"))
        for agent, note in items.items():
            body.append(f"\n{display(agent)}: ", style=f"bold {color(agent)}")
            body.append(str(note))
        if not body.plain.strip():
            body.append("Everyone is heading the right way." if good else "(no details)")
        return Panel(body, title=title, border_style="green" if good else "yellow", title_align="left")

    def _stages_table(self, stages: list[dict[str, Any]], start: int = 1) -> Any:
        from rich.console import Group
        icons = {"research": "🔎", "build": "🛠", "debate": "⚖", "simulate": "🎲"}
        parts: list[Text] = []

        def row(label: str, body: str, color: str = "", dim: str = "") -> None:
            t = Text("   ")
            t.append(label, style=f"bold {color}".strip())
            t.append(" " + body)
            if dim:
                t.append("\n      " + dim, style="dim")
            parts.append(t)

        for i, st in enumerate(stages, start):
            head = Text(f"{i}. {icons.get(st['type'], '')} {st['name']}", style="bold")
            head.append(f"  [{st['type']}] {st.get('goal', '')}", style="dim")
            parts.append(head)
            if st["type"] in ("research", "build"):
                for a in st["assignments"]:
                    row(display(a["agent"]), f"({a['role']}): {a['task']}", color(a["agent"]),
                        f"owns: {', '.join(a['owns'])}" if a["owns"] else "")
            elif st["type"] == "debate":
                row("Question:", f"{st['topic']} ({st['rounds']} rounds)")
                for agent, pos in st["positions"].items():
                    row(display(agent), f"argues: {pos}", color(agent))
            else:
                for agent, actor in st["actors"].items():
                    row(display(agent), f"plays {actor}", color(agent))
                row("Game master:", f"{display(st['game_master'])}, {st['turns']} turns")
                for sc in st["scenarios"]:
                    row(f"🎲 {sc['name']}:", sc["setup"])
            if st.get("skills"):
                parts.append(Text(f"   skills: {', '.join(st['skills'])}", style="dim"))
            parts.append(Text(""))
        return Group(*parts)

    def _brief_panel(self, plan: dict[str, Any], lead: str) -> Panel:
        from rich.console import Group
        parts: list[Any] = [Markdown(plan.get("client_brief", "").replace("<", "\\<"))]
        if plan.get("questions"):
            q = Text("\nWhat I need from you", style="bold")
            for i, question in enumerate(plan["questions"], 1):
                q.append(f"\n  {i}. {question}", style="")
            q.append("\n\nAnswer in one message (e.g. \"1: yes, 2: Korea only\"). Anything you skip, the team decides.", style="dim")
            parts.append(q)
        team = plan.get("team") or []
        hires = {h["name"] for h in plan.get("hires", [])}
        if team:
            t = Text("\nTeam: ", style="dim")
            for i, m in enumerate(team):
                t.append(("" if i == 0 else ", ") + display(m) + (" (new hire)" if m in hires else ""), style=color(m))
            parts.append(t)
        parts.append(Text("Enter to start · type answers or changes · /details for the full plan", style="dim italic"))
        return Panel(Group(*parts), title=f"📋 {display(lead)}'s plan: {plan['project_name']}",
                     border_style=color("triad"), title_align="left")

    def _plan_panel(self, plan: dict[str, Any], lead: str) -> Panel:
        md = (f"**{plan['project_name']}**: {plan['summary']}\n\n**Deliverable:** {plan['deliverable']}\n\n"
              f"**Conventions**\n\n{plan['architecture']}").replace("<", "\\<")
        if plan.get("verify"):
            md += f"\n\n**Verify with** `{plan['verify']}`"
        from rich.console import Group
        return Panel(Group(Markdown(md), Text(""), Text("Workflow", style="bold"), self._stages_table(plan["stages"])),
                     title=f"📋 Plan by {display(lead)}", border_style=color("triad"), title_align="left")

    def _usage_table(self) -> Table:
        table = Table(title="Usage this project", title_justify="left")
        for col in ("Member", "Calls", "Tokens", "Time", "Subscription quota"):
            table.add_column(col)
        if self.company:
            for m, u in self.company.member_usage.items():
                if u.calls:
                    quota = " ".join(f"{k} {v:.0%}" for k, v in self.company.usage[provider_of(m)].quota.items()) or "—"
                    table.add_row(Text(display(m), style=color(m)), str(u.calls), f"{u.tokens:,}", f"{u.seconds / 60:.1f} min", quota)
        return table
