"""Everything Triad says to the agents."""
from __future__ import annotations

import json
from typing import Any

from .agents import DISPLAY, display, provider_of

# Each model brings a different strength. These are nudges, not cages: the lead can assign anything to anyone.
STRENGTHS = {
    "claude": "deep reasoning, analysis and writing, architecture and clean APIs",
    "codex": "precise implementation, data processing, quantitative work and tooling",
    "antigravity": "web research and browsing, frontend/visual work, broad world knowledge",
}

MARK = "TRIAD_ROLE"  # first line of every prompt; also lets the offline demo agent know what to do

STAGE_TYPES = {
    "research": "gather facts and data (web, documents, APIs, files) and write sourced notes",
    "build": "create or change artifacts: code, websites, datasets, scripts, documents, reports",
    "debate": "members argue assigned positions in rounds to stress-test conclusions and find the cruxes",
    "simulate": "role-play scenarios: members play real-world actors, a game master adjudicates turn by turn",
}

GUIDES = {
    "research": """RESEARCH STANDARDS
- Use web search / page reading for anything current; today's facts beat your training data. Note the date of each source.
- Cite every non-obvious claim inline with a source (publication, title, URL). If a link is a redirect, cite publication + headline.
- Separate FACTS (sourced), ESTIMATES (with reasoning) and OPINION. Say what you could not verify.
- Prefer primary sources and data; collect numbers in tables or CSV when useful. Note conflicting reports.
- Write for teammates who will build on your notes: lead with key findings, then detail.""",
    "build": """BUILD STANDARDS
- Make it real and working: run it, test it, fix it. Keep dependencies and complexity low.
- Follow the shared conventions/contracts exactly; announce any interface you define.""",
    "debate": """DEBATE STANDARDS
- Argue your assigned position well, but honestly: use evidence from the team's notes and the web, cite it.
- Engage the strongest version of the other arguments; concede points that are right.
- Be concrete: probabilities, magnitudes, timelines, mechanisms. Name the cruxes that would change your mind.""",
    "simulate": """SIMULATION STANDARDS
- Stay in character as your actor: pursue their real interests, constraints, information and style.
- Be realistic, not dramatic: actors are usually cautious, bound by institutions, and react to incentives.
- Make concrete moves (statements, votes, deals, sanctions, market moves) with brief private reasoning.""",
}


def _team_lines(team: list[str]) -> str:
    lines = []
    for a in team:
        shared = [m for m in team if m != a and provider_of(m) == provider_of(a)]
        note = f" [shares one {display(provider_of(a))} subscription with {', '.join(shared)}]" if shared else ""
        lines.append(f"- {a} ({display(a)}): strong at {STRENGTHS[provider_of(a)]}{note}")
    return "\n".join(lines)


def staffing_text(team: list[str], allowed: list[str], max_team: int, next_names: dict[str, str]) -> str:
    hires = ", ".join(f"{p} (would join as {next_names[p]})" for p in allowed)
    return f"""STAFFING: you may change the team. Currently {len(team)} member(s); at most {max_team}.
- "add": hire extra members when the work genuinely parallelises into more pieces than people. You may hire
  only these providers: {hires}. Members of the same provider share one subscription's allowance, so hiring
  a second member of a provider does not add allowance, only parallelism.
- "bench": drop members whose skills the remaining work doesn't need (at least one member must remain).
Most projects need no staffing change; every member costs usage."""


def _skills_text(skills: dict[str, str], names: list[str] | None) -> str:
    chosen = [n for n in (names or []) if n in skills]
    if not chosen:
        return ""
    return "\n\nSKILLS FOR THIS STAGE\n" + "\n\n".join(f"## {n}\n{skills[n]}" for n in chosen)


def prior_text(prior: dict[str, Any] | None) -> str:
    """A finished project in the same folder: the new request usually builds on it."""
    if not prior:
        return ""
    stages = "\n".join(f"- {h['name']} ({h['type']}): {h['summary'][:400]}" for h in prior.get("history", []))
    asks = "; ".join(prior.get("plan", {}).get("client_input", []))
    return f"""
THIS FOLDER ALREADY CONTAINS AN EARLIER PROJECT. The new request most likely builds on it; reuse and
extend what exists (read README.md / DELIVERY.md and the files) instead of starting over, unless told otherwise.
Earlier request: {prior.get('request', '')[:600]}
Delivered: {prior.get('plan', {}).get('deliverable', '')}
What its stages established:
{stages or '(no stage summaries)'}
{('Client requirements then: ' + asks) if asks else ''}
Lead's delivery note: {str(prior.get('delivery', ''))[:800]}
"""


def brainstorm(agent: str, team: list[str], request: str, prior: dict[str, Any] | None = None) -> str:
    return f"""{MARK}: brainstorm
You are {display(agent)}, one of {len(team)} AI members who together run a small, versatile company: you
take on software, research, analysis, data and content work.
The team:
{_team_lines(team)}

A client has asked for:
<<<
{request}
>>>
{prior_text(prior)}
Before the lead writes the plan, give your own independent take. If the current directory has relevant
files, look at them (read-only: read files, or use ls/cat/grep/find/git log; other commands are blocked).
You may search the web if the request depends on current events. Do not modify anything.
Be concrete and brief (under 300 words):
1. What the client really needs, and what an excellent result looks like (the deliverable).
2. How you'd approach it: which kinds of work it needs ({", ".join(STAGE_TYPES)}) and in what order.
3. Risks, ambiguities, and pitfalls.
4. Which part you would most like to own, and why it fits your strengths.
"""


def _stage_menu() -> str:
    return "\n".join(f"- {k}: {v}" for k, v in STAGE_TYPES.items())


PLAN_SCHEMA = """{
  "client_brief": "what the client sees when approving: plain language, no jargon, markdown, under 150 words. Say what they will get, the steps in one line each, who does what, and roughly how long/heavy it is",
  "questions": ["only what you genuinely need from the client: decisions, preferences, files, access, or things you are assuming. Each short and answerable; [] if nothing"],
  "project_name": "short-kebab-name",
  "summary": "2-4 sentences: what we are delivering",
  "deliverable": "exactly what the client receives (files/folders) at the end",
  "architecture": "shared conventions everyone follows: folder layout, file formats, contracts between parts, citation style",
  "stages": [
    {"name": "Data collection", "type": "research", "goal": "what this stage must produce",
     "skills": ["optional skill names"],
     "assignments": [{"agent": "<member id>", "role": "job title", "task": "detailed task",
                      "owns": ["paths this member writes, e.g. work/01-data/claude.md"], "deliverables": ["..."]}]},
    {"name": "Debate", "type": "debate", "goal": "...", "topic": "the question being debated", "rounds": 2,
     "positions": {"<member id>": "the stance this member argues"}},
    {"name": "Scenario simulation", "type": "simulate", "goal": "...", "turns": 3, "game_master": "<member id>",
     "actors": {"<member id>": "the real-world actor(s) this member plays"},
     "scenarios": [{"name": "short name", "setup": "starting situation and what is at stake"}]}
  ],
  "verify": "optional shell command that checks built artifacts work (tests or a smoke run), else empty",
  "staffing": {"add": [{"provider": "codex", "reason": "why"}], "bench": ["<member id>"]}
}"""


def plan(lead: str, team: list[str], request: str, proposals: dict[str, str], feedback: str = "",
         previous: dict[str, Any] | None = None, source: str = "client", skills: dict[str, str] | None = None,
         staffing: str = "", prior: dict[str, Any] | None = None) -> str:
    ideas = "\n\n".join(f"### {display(a)}'s proposal\n{text}" for a, text in proposals.items()) or "(no proposals; plan from scratch)"
    revision = ""
    if previous:
        intro = ("The client reviewed your previous plan and asked for changes." if source == "client" else
                 "The CLIENT CHANGED DIRECTION mid-project and you halted the team. Work already done is in the "
                 "workspace: look at it, keep what still serves the new direction, and plan the remaining work."
                 if source == "pivot" else
                 "You (the lead) HALTED the team mid-project because the plan had a critical problem. Work already "
                 "done is in the workspace: look at it, keep what is good, and plan only the remaining work.")
        revision = f"""
{intro}
Previous plan:
{json.dumps(previous, indent=2)}
{"Client feedback / answers to your questions" if source == "client" else "The client's new direction" if source == "pivot" else "Why the team was halted"}: {feedback}
Produce a revised plan that fixes this. Drop questions the client has answered.
"""
    catalog = "\n".join(f"- {n}: {t.strip().splitlines()[0][:160]}" for n, t in (skills or {}).items()) or "(none)"
    return f"""{MARK}: plan
You are the lead (managing director) of a {len(team)}-person AI company that takes on any kind of work:
{_team_lines(team)}

Client request:
<<<
{request}
>>>

{prior_text(prior)}
Each member independently proposed an approach:
{ideas}
{revision}
Design the project. You choose the workflow: an ordered list of STAGES, each of one of these types:
{_stage_menu()}
Every member has web search, can read and write files, and can run commands in the workspace.

Guidance:
- Use the simplest workflow that produces an excellent result. Pure software usually needs one "build"
  stage. An analysis or forecast might be research -> debate -> simulate. A data product might be
  research -> build. Mixed projects can split one stage across different kinds of work
  (e.g. one member researches while two build). At most 5 stages; fewer is better.
- After the last stage you (the lead) always do final assembly: you compile the deliverable from
  everything produced. Only add a writing/build stage at the end if several members should write it.
- research/build stages: exactly one assignment per active member (the team above, plus anyone you hire,
  minus anyone you bench), roughly EQUAL effort (usage allowances are limited, nobody idle or overloaded),
  non-overlapping "owns". Spread work across subscriptions, not just members.
  Research notes go under work/<NN>-<stage-slug>/.
- debate stages: give each member a distinct, useful position (e.g. base case / strongest alternative /
  devil's advocate). 1-3 rounds.
- simulate stages: these are role-play wargames, not code. 1-3 scenarios, 2-4 turns each; give each member
  actors to play; the game master (default: you) adjudicates each turn. Each turn costs one call per member
  plus one for the game master, so be economical.
- Combine the best ideas from all proposals; diversity of the team is the point.
- Available skills you may attach to stages by name:
{catalog}

{staffing}

The client approves the plan from "client_brief" and "questions" alone, so write those for a busy
non-expert: what they get and what you need from them, not your internal reasoning. Put a question only
where the answer would change the work; otherwise make a sensible assumption and mention it in the brief.

Reply with ONLY a JSON object (no prose, no code fences) matching:
{PLAN_SCHEMA}
"""


def _plan_brief(p: dict[str, Any]) -> str:
    lines = [f"Project: {p.get('project_name', '')}", f"Summary: {p.get('summary', '')}",
             f"Deliverable: {p.get('deliverable', '')}", "", "Shared conventions:", str(p.get("architecture", "")), "",
             "Workflow:"]
    for i, s in enumerate(p.get("stages", []), 1):
        lines.append(f"{i}. {s['name']} ({s['type']}): {s.get('goal', '')}")
    if p.get("verify"):
        lines += ["", f"Verification command: {p['verify']}"]
    if p.get("client_input"):
        lines += ["", "The client's answers and requests (binding):"] + [f"- {x}" for x in p["client_input"]]
    elif p.get("questions"):
        lines += ["", "Open questions the client left to the team's judgment: " + "; ".join(p["questions"])]
    return "\n".join(lines)


def _history(history: list[dict[str, Any]]) -> str:
    if not history:
        return "(this is the first stage)"
    return "\n".join(f"- Stage {h['index']} {h['name']} ({h['type']}), outputs in {h['folder']}:\n  {h['summary']}"
                     for h in history)


def _comms(agent: str, team: list[str], lead: str) -> str:
    others = [a for a in team if a != agent]
    tool = f"python3 .triad/msg.py --as {agent}"
    return f"""TEAM CHAT. Your teammates are working AT THE SAME TIME as you, in this same directory.
The lead ({display(lead)}, as manager) oversees the project and answers questions.
  {tool} checkin "<2-4 lines: what you will produce, where, and how>"
      Do this ONCE, early: after you have looked around and before producing much. The lead uses it to
      catch wrong directions before they cost the team time.
  {tool} ask "<question>"           # ask the LEAD; blocks until it answers (~1 min)
  {tool} send <who> "<message>"     # who = {", ".join(others)}, or all
  {tool} inbox                      # read new messages addressed to you
When to use what (every message costs the team time and usage, so be purposeful):
- ask the lead only about genuine ambiguity in the plan or the client's intent that would change what
  you produce or how parts fit. Decide small details yourself. Don't ask what the plan already answers.
- send a teammate a message when you find or define something they depend on (a key fact, a data
  format, an interface), or need something from their area. Ask instead of editing their files.
- Check your inbox when you start, after each milestone, and before you finish. Messages from "lead"
  are instructions: follow them. The client ("user") may post too; treat that as requirements.
- Never wait on a teammate more than ~2 minutes: assume sensibly, say what you assumed, keep going."""


HANDOFF = """When you are done, your FINAL reply must be a handoff report in exactly this shape:
STATUS: READY | CHANGED | BLOCKED
## What I produced (files)
## Key findings / interfaces others rely on
## How to use / verify it
## Open issues and uncertainties
(READY = complete and fits with the rest; CHANGED = I made changes this round; BLOCKED = cannot finish without something, say what.)"""


def work(agent: str, team: list[str], lead: str, request: str, p: dict[str, Any], stage: dict[str, Any],
         index: int, assignment: dict[str, Any], history: list[dict[str, Any]], skills: dict[str, str],
         note: str = "") -> str:
    return f"""{MARK}: work
STAGE TYPE: {stage['type']}
You are {display(agent)}, {assignment.get('role', 'team member')} in a {len(team)}-person AI company.
{note}
Original client request:
<<<
{request}
>>>

{_plan_brief(p)}

PROJECT SO FAR:
{_history(history)}

CURRENT STAGE {index}: {stage['name']} ({stage['type']}). Goal: {stage.get('goal', '')}

YOUR ASSIGNMENT
Task: {assignment.get('task', '')}
You own (write freely): {', '.join(assignment.get('owns', [])) or 'the files your task needs'}
Deliverables: {'; '.join(assignment.get('deliverables', [])) or 'see task'}
Do not edit files owned by teammates. Do not run git commit, git checkout or anything that rewrites history.
Do not touch .triad/ except through the chat tool.

{GUIDES[stage['type']]}{_skills_text(skills, stage.get('skills'))}

{_comms(agent, team, lead)}

{HANDOFF}
"""


def sync(agent: str, team: list[str], lead: str, request: str, p: dict[str, Any], stage: dict[str, Any],
         assignment: dict[str, Any], handoffs: dict[str, str], transcript: str, round_no: int) -> str:
    reports = "\n\n".join(f"### {display(a)}'s latest handoff\n{h}" for a, h in handoffs.items())
    return f"""{MARK}: sync
You are {display(agent)}, {assignment.get('role', 'team member')}. Integration round {round_no} of stage
"{stage['name']}": everyone delivered a first version and now we fit the pieces together into one product.

Original client request:
<<<
{request}
>>>

{_plan_brief(p)}

Your area: {', '.join(assignment.get('owns', [])) or assignment.get('task', '')}

Latest handoff reports from the whole team:
{reports}

Team chat so far:
{transcript}

Now:
1. Read your inbox. Inspect how teammates' work uses yours and vice versa; run it.
2. Fix mismatches, bugs and gaps IN YOUR AREA. If the fix belongs to someone else, message them with
   the exact problem; they are doing this same step right now.
3. If everything in your area already works end to end, change nothing and report STATUS: READY.

{_comms(agent, team, lead)}

{HANDOFF}
"""


def direction(lead: str, request: str, p: dict[str, Any], stage: dict[str, Any], checkins: dict[str, str],
              transcript: str, replans: int, max_replans: int) -> str:
    notes = "\n".join(f"- {a}: {t}" for a, t in checkins.items())
    return f"""{MARK}: direction
You are {display(lead)}, the lead (manager) of an AI company. The team started stage "{stage['name']}"
({stage['type']}) a few minutes ago. Each member has checked in with their plan of attack. Your job: catch
wrong directions EARLY, cheaply.

Client request:
<<<
{request}
>>>

{_plan_brief(p)}

Check-ins:
{notes}

Team chat so far:
{transcript}

Decide, conservatively. Most of the time the right answer is "on_track" with no notes.
- "adjust": someone's approach is slightly off (wrong file, missing requirement, mismatch with a teammate).
  Give them a short, specific note. They keep working.
- "halt" a member only if continuing would waste significant work (producing the wrong thing, wrong
  approach, contradicting the conventions). Say exactly what to do instead.
- "halt_all" only if the PLAN ITSELF is fundamentally wrong for the client's request, so everyone must
  stop for a revised plan. Replans used: {replans} of {max_replans}. If none are left, use notes instead.

Reply with ONLY a JSON object:
{{"verdict": "on_track" | "adjust" | "halt",
  "notes": {{"<member id>": "short correction"}},
  "halt": [{{"agent": "<member id>", "reason": "what is wrong and what to do instead"}}],
  "halt_all": "" }}
"""


def answer(lead: str, request: str, p: dict[str, Any], questions: list[dict[str, Any]], transcript: str,
           replans: int, max_replans: int) -> str:
    qs = "\n".join(f"- from {q['from']}: {q['text']}" for q in questions)
    return f"""{MARK}: answer
You are {display(lead)}, the lead (manager) of an AI company that is working right now.
Members are blocked waiting on your answers, so be quick and decisive.

Client request:
<<<
{request}
>>>

{_plan_brief(p)}

Recent team chat:
{transcript}

Questions for you:
{qs}

Answer every question in 1-4 sentences, consistent with the plan and the client's intent. If your answer
changes or clarifies something other members depend on, address it to "all" so everyone hears it.
If a question reveals that someone is going in a costly wrong direction, you may halt them (they restart
with your correction). Only if it reveals that the plan itself is fundamentally wrong may you set
halt_all (replans used: {replans} of {max_replans}).

Reply with ONLY a JSON object:
{{"answers": [{{"to": "<member id or all>", "text": "..."}}],
  "halt": [{{"agent": "<member id>", "reason": "..."}}],
  "halt_all": "" }}
"""


GATE_FOCUS = {
    "build": """Judge COHESION and fitness: does it do what this stage and the client need? Do the parts fit
(interfaces, formats, naming, one coherent style/UX, no duplicated or contradictory logic)? Does it run, and
do tests test real behaviour?""",
    "research": """Judge the evidence: are the key questions answered, claims sourced and current, facts separated
from estimates, numbers consistent across members' notes, and important gaps or contradictions flagged?""",
    "debate": """Synthesize: where did the members converge, which disagreements survived (the cruxes), which
arguments were strongest and why, and what probabilities or judgments the team now holds.""",
    "simulate": """Synthesize: across scenarios, what happened and why, which outcomes look robust vs fragile,
which actor behaviours drove results, surprises, and what signposts in the real world would indicate each path.""",
}


def gate(lead: str, team: list[str], request: str, p: dict[str, Any], stage: dict[str, Any], index: int,
         folder: str, handoffs: dict[str, str], transcript: str, verify_output: str, files: str,
         round_no: int, max_rounds: int, remaining: list[dict[str, Any]], replans: int, max_replans: int,
         skills: dict[str, str], staffing: str = "") -> str:
    reports = "\n\n".join(f"### {display(a)}'s handoff\n{h[:2500]}" for a, h in handoffs.items()) or "(see the stage files)"
    fixable = stage["type"] in ("research", "build")
    remaining_json = json.dumps(remaining, indent=2) if remaining else "[] (this was the last stage; final assembly is next)"
    catalog = ", ".join(skills) or "(none)"
    decide = ('- "approved"/"feedback": approve unless there are real problems. No nitpicks: every fix round costs time '
              'and usage. Feedback must be concrete (file, what, what to do) and only for members who must act '
              f'({", ".join(team)}).') if fixable else \
        '- "approved": true (this stage type is not reworked; capture its results in the summary).'
    return f"""{MARK}: gate
You are {display(lead)}, the lead (manager). Stage {index} "{stage['name']}" ({stage['type']}) just finished.
Review it before the project moves on. Review round {round_no} of {max_rounds}.

Client request:
<<<
{request}
>>>

{_plan_brief(p)}

Stage goal: {stage.get('goal', '')}
Stage outputs are in: {folder} (and the files listed below). Read them; do not modify anything.

Files in the project:
{files}

Verification run (just now):
{verify_output or '(no verification command)'}

Handoffs:
{reports}

Team chat (recent):
{transcript}

{GATE_FOCUS[stage['type']]}

Then decide:
- "summary": 3-8 sentences capturing what this stage established; later stages and the final deliverable
  rely on it, so include the key findings, numbers and judgments, not just "done".
{decide}
- "next_stages": what we learned may change what comes next. Return null to keep the remaining stages
  as planned, or a full replacement list (same schema as the plan's stages; may be empty) to add, drop or
  change them. Change them only for a good reason. Skills available: {catalog}.
- "halt_all": only if the whole project is off-target and needs a new plan (replans used: {replans} of {max_replans}).
- "staffing": usually omit. If the remaining work needs more or fewer people, hire/bench (see below); new members
  can be given assignments in "next_stages".

Team: {", ".join(team)}
{staffing}

Remaining stages as planned:
{remaining_json}

Reply with ONLY a JSON object:
{{"approved": true | false,
  "summary": "...",
  "feedback": {{"<member id>": "specific fixes"}},
  "next_stages": null,
  "halt_all": "",
  "staffing": {{"add": [], "bench": []}} }}
"""


def fix(agent: str, team: list[str], lead: str, request: str, p: dict[str, Any], stage: dict[str, Any],
        assignment: dict[str, Any], mine: str, others: dict[str, str], skills: dict[str, str], note: str = "") -> str:
    theirs = "\n".join(f"- {a}: {t}" for a, t in others.items() if a != agent) or "(nobody else)"
    return f"""{MARK}: fix
STAGE TYPE: {stage['type']}
You are {display(agent)}, {assignment.get('role', 'team member')}. The lead reviewed stage "{stage['name']}"
and needs changes in your area before the project moves on.
{note}
Original client request:
<<<
{request}
>>>

{_plan_brief(p)}

Your area: {', '.join(assignment.get('owns', [])) or assignment.get('task', '')}

THE LEAD'S FEEDBACK FOR YOU:
{mine}

Other members were asked to fix (they are working right now; coordinate if it touches you):
{theirs}

Make exactly these fixes, verify them, and do not start unrelated work.

{GUIDES[stage['type']]}{_skills_text(skills, stage.get('skills'))}

{_comms(agent, team, lead)}

{HANDOFF}
"""


def debate_turn(agent: str, team: list[str], request: str, p: dict[str, Any], stage: dict[str, Any], index: int,
                position: str, round_no: int, rounds: int, transcript: str, history: list[dict[str, Any]],
                skills: dict[str, str]) -> str:
    phase = ("OPENING STATEMENT: make your case." if round_no == 1 else
             "CLOSING STATEMENT: respond to the others, state your final view with probabilities, what you "
             "changed your mind on, and the remaining cruxes." if round_no == rounds else
             "REBUTTAL: answer the strongest points against you, press on the weakest points of the others.")
    return f"""{MARK}: debate
You are {display(agent)}, in a structured debate inside an AI company (round {round_no} of {rounds}).

Client request:
<<<
{request}
>>>

{_plan_brief(p)}

PROJECT SO FAR:
{_history(history)}
You may read files in the workspace (earlier stages' notes) and search the web for evidence. Do not modify files.

DEBATE (stage {index}: {stage['name']})
Question: {stage.get('topic') or stage.get('goal', '')}
YOUR ASSIGNED POSITION: {position}

Debate so far:
{transcript or '(you are opening)'}

{phase}
Under 350 words. {GUIDES['debate']}{_skills_text(skills, stage.get('skills'))}

End with exactly one line:
POSITION: HOLD | SHIFTED | CONVERGED
(CONVERGED = you now substantially agree with the others and further rounds would add little.)
"""


def sim_move(agent: str, request: str, p: dict[str, Any], stage: dict[str, Any], scenario: dict[str, str],
             actor: str, state: str, log: str, turn: int, turns: int, history: list[dict[str, Any]],
             skills: dict[str, str]) -> str:
    return f"""{MARK}: simulate_move
You are {display(agent)}, playing a role in a scenario simulation (a wargame) run by an AI company to
explore how events could unfold. Turn {turn} of {turns}.

Client request (what the simulation informs):
<<<
{request}
>>>

PROJECT SO FAR:
{_history(history)}

SCENARIO: {scenario.get('name', '')}
{scenario.get('setup', '')}

YOU PLAY: {actor}

Current state of the world (from the game master):
{state}

What has happened so far:
{log or '(this is the first turn)'}

As {actor}, decide your move this turn. Reply in this shape (under 250 words):
PRIVATE REASONING: what you want, what you fear, what you believe the others will do
MOVE: the concrete actions, statements or decisions you take now (if you play several actors, one MOVE per actor)
{GUIDES['simulate']}{_skills_text(skills, stage.get('skills'))}
"""


def sim_adjudicate(gm: str, request: str, stage: dict[str, Any], scenario: dict[str, str], state: str,
                   log: str, moves: dict[str, str], turn: int, turns: int) -> str:
    made = "\n\n".join(f"### {who}\n{text}" for who, text in moves.items())
    return f"""{MARK}: simulate_gm
You are {display(gm)}, the GAME MASTER of a scenario simulation. Turn {turn} of {turns}.
Your job is to resolve the actors' moves realistically: what actually happens when these moves meet the world
and each other, including second-order effects, public reaction, markets, and luck. Be impartial and plausible;
don't make everything succeed or fail.

Client request (what the simulation informs):
<<<
{request}
>>>

SCENARIO: {scenario.get('name', '')}
{scenario.get('setup', '')}

State before this turn:
{state}

History:
{log or '(first turn)'}

Moves this turn:
{made}

Reply with ONLY a JSON object:
{{"narrative": "what happened this turn, 80-200 words",
  "state": "the updated state of the world for the next turn, concise",
  "ended": false,
  "outcome": "if ended or this is the last turn: how the scenario resolved and why; else empty"}}
"""


def integrate(lead: str, team: list[str], request: str, p: dict[str, Any], history: list[dict[str, Any]],
              transcript: str, verify_output: str, open_notes: str = "") -> str:
    return f"""{MARK}: integrate
You are {display(lead)}, the lead. Your team has finished all stages. You are now assembling the final
deliverable for the client.

Client request:
<<<
{request}
>>>

{_plan_brief(p)}

WHAT EACH STAGE PRODUCED:
{_history(history)}

Team chat (recent):
{transcript}

Verification run before you started:
{verify_output or '(no verification command configured)'}

Notes from your reviews that were not yet addressed:
{open_notes or '(none)'}

Do this:
1. Read the stage outputs. Produce the deliverable described above, complete and polished. Fix remaining
   problems and unaddressed notes (you may edit any file now; keep changes purposeful). If something was
   built, run it.
2. Make sure there is a README.md that tells the client what is here and how to use it.
3. Write DELIVERY.md: what was delivered, who did what, how to use it, confidence and known limitations.
4. Do not run git commit.
Your FINAL reply: a short message to the client (under 250 words) summarising the delivery and where to start.
"""


def client_desk(lead: str, request: str, p: dict[str, Any], stage: dict[str, Any] | None, index: int,
                history: list[dict[str, Any]], status: str, transcript: str, message: str, can_replan: bool) -> str:
    where = f"stage {index} of {len(p.get('stages', []))}: {stage['name']} ({stage['type']})" if stage else "between stages"
    return f"""{MARK}: client_desk
You are {display(lead)}, the lead (managing director) of an AI company. The CLIENT has just messaged YOU,
privately: the team did not see it. The team is working right now ({where}).

Original request:
<<<
{request}
>>>

{_plan_brief(p)}

Finished stages:
{_history(history)}

What each member is doing right now:
{status}

Recent team chat:
{transcript}

THE CLIENT'S MESSAGE:
<<<
{message}
>>>

You may read files in the workspace to answer accurately; do not modify anything.
1. "reply": answer the client directly in plain language (under 150 words). If they ask about progress,
   summarize what is done, what each member is doing now, and what comes next. No internal jargon.
2. If they want something changed, pick the LEAST disruptive action that fully achieves it:
   - "notes": short instructions to specific members, who adjust while they keep working;
   - "halt": stop specific members now and restart them with a correction (their work so far is kept);
   - "next_stages": replace the stages after the current one (same schema as the plan's stages);
   - "replan": a reason, only for a real change of direction that makes the current work wrong; everyone
     stops, you write a revised plan, and the client approves it.{"" if can_replan else " (Not possible right now: use the other actions.)"}
   Say in your reply what you are doing about it.
3. "requirement": if the message contains an instruction for the work, restate it as one binding requirement
   the whole team will follow from now on; otherwise "".

Reply with ONLY a JSON object:
{{"reply": "...", "requirement": "", "notes": {{}}, "halt": [], "next_stages": null, "replan": ""}}
"""


def narrate(reporter: str, request: str, p: dict[str, Any], previous: str, new_lines: list[str]) -> str:
    feed = "\n".join(new_lines[-120:])
    return f"""{MARK}: narrate
You are {display(reporter)}, the company's progress reporter. The client is watching a live project and wants
a calm, plain-language picture of how it is going, not the raw log. Do not use any tools; just write.

Project: {p.get('project_name') or request[:200]}
{('Workflow: ' + ' -> '.join(s['name'] for s in p.get('stages', []))) if p.get('stages') else ''}

Your previous report:
{previous or '(none yet)'}

New activity since then (raw log, terse):
{feed}

Write the next report: 2-4 short sentences. What is happening right now and who is doing it, what got done,
and any problem, decision or risk worth knowing. Skip routine noise (file reads, inbox checks). No preamble,
no bullet points, no repetition of your previous report.
"""
