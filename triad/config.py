from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .agents import AGENTS


def config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "triad" / "config.json"


@dataclass
class AgentConfig:
    enabled: bool = True
    model: str = ""        # empty = the CLI's own default
    executable: str = ""   # empty = claude / codex / agy on PATH


@dataclass
class Config:
    team: list[str] = field(default_factory=list)  # e.g. ["claude", "codex", "claude"] or "codex:model"; empty = one of each installed CLI
    max_team: int = 5               # the lead may hire up to this many members in total
    lead: str = "claude"            # who plans and does final integration (a member or provider name)
    brainstorm: bool = True         # every agent proposes an approach before planning
    sync_rounds: int = 1            # peer integration rounds after the first build (stops early when all READY)
    review_rounds: int = 2          # lead review -> fix cycles before final assembly
    direction_check: bool = True    # lead checks everyone's early plan of attack once per build
    max_replans: int = 1            # how many times the lead may halt everyone for a revised plan
    max_halts_per_agent: int = 2    # how many times the lead may stop and redirect one engineer
    lead_answer_budget: int = 8     # lead calls for answering questions per project (questions are batched)
    question_batch_seconds: float = 8.0  # wait this long to batch questions arriving together
    checkin_timeout: int = 300      # run the direction check after this long even if someone never checked in
    poll_seconds: float = 1.0
    narrator: str = "auto"          # who writes progress reports: "auto" (prefers antigravity), a member/provider, or "off"
    narrator_interval: float = 60   # at most one report per this many seconds, and only when something happened
    view: str = "summary"           # app view: "summary" (reports + key events) or "detail" (everything)
    turn_timeout: int = 2400        # seconds per agent turn
    verify_timeout: int = 600
    codex_network: bool = True      # let Codex's sandbox reach the network (package installs)
    web: bool = True                # web search/fetch for every member (Claude WebSearch, codex --search, agy search_web)
    skills_dir: str = ""            # extra skills folder (default ~/.config/triad/skills)
    agents: dict[str, AgentConfig] = field(default_factory=lambda: {a: AgentConfig() for a in AGENTS})

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_path()
        cfg = cls()
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            return cfg
        for key in ("lead", "brainstorm", "sync_rounds", "turn_timeout", "verify_timeout", "codex_network",
                    "review_rounds", "direction_check", "max_replans", "max_halts_per_agent", "lead_answer_budget",
                    "question_batch_seconds", "checkin_timeout", "poll_seconds", "web", "skills_dir", "max_team", "narrator", "narrator_interval", "view"):
            if key in raw:
                current = getattr(cfg, key)
                value = raw[key]
                setattr(cfg, key, (str(value).lower() in ("1", "true", "yes", "on")) if isinstance(current, bool) else type(current)(value))
        if isinstance(raw.get("team"), list):
            cfg.team = [str(x) for x in raw["team"]]
        elif isinstance(raw.get("team"), str):
            cfg.team = [x.strip() for x in raw["team"].split(",") if x.strip()]
        for name, values in (raw.get("agents") or {}).items():
            if name in cfg.agents and isinstance(values, dict):
                cfg.agents[name] = AgentConfig(**{k: v for k, v in values.items() if k in AgentConfig.__dataclass_fields__})
        return cfg

    def save(self, path: Path | None = None) -> Path:
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")
        return path
