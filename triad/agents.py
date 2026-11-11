"""The three teammates: how to launch each vendor CLI and read its live output.

Each provider is driven through its official, subscription-authenticated CLI in
non-interactive mode. We stream the CLI's JSON events so the UI can show what
each agent is doing in real time, and we keep a running usage tally so the
team can spread work across all three subscriptions.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

AGENTS = ("claude", "codex", "antigravity")
EXECUTABLES = {"claude": "claude", "codex": "codex", "antigravity": "agy"}
DISPLAY = {"claude": "Claude", "codex": "Codex", "antigravity": "Antigravity"}
def provider_of(name: str) -> str:
    """Members are named after their provider: claude, claude-2, codex-3 ... -> claude / codex."""
    base = re.sub(r"-\d+$", "", name)
    return base if base in AGENTS else name


def display(name: str) -> str:
    provider = provider_of(name)
    if provider in DISPLAY and provider != name:
        return f"{DISPLAY[provider]} #{name.rsplit('-', 1)[1]}"
    return DISPLAY.get(name, name)


def color(name: str) -> str:
    return COLORS.get(provider_of(name), COLORS.get(name, "white"))


def name_members(spec: list[str]) -> list[tuple[str, str, str]]:
    """["claude", "codex:gpt-6", "claude"] -> [("claude","claude",""), ("codex","codex","gpt-6"), ("claude-2","claude","")]."""
    counts: dict[str, int] = {}
    out = []
    for item in spec:
        provider, _, model = str(item).strip().partition(":")
        provider = provider.lower().strip()
        provider = {"gemini": "antigravity", "agy": "antigravity"}.get(provider, provider)
        if provider not in AGENTS:
            continue
        counts[provider] = counts.get(provider, 0) + 1
        name = provider if counts[provider] == 1 else f"{provider}-{counts[provider]}"
        out.append((name, provider, model.strip()))
    return out


READONLY_COMMANDS = ("ls", "cat", "head", "tail", "find", "grep", "rg", "wc", "tree", "pwd", "file", "stat",
                     "git log", "git status", "git diff", "git show", "git ls-files")
COLORS = {"claude": "#d97757", "codex": "#10a37f", "antigravity": "#4285f4",
          "user": "#e5c07b", "triad": "#c678dd", "all": "#abb2bf", "lead": "#e06c75"}


@dataclass
class Activity:
    """One live thing an agent did: ran a command, edited a file, said something."""
    agent: str
    kind: str  # "tool" | "text" | "info" | "error"
    text: str


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    quota: dict[str, float] = field(default_factory=dict)  # e.g. {"5h": 0.06, "7d": 0.05}
    exhausted_until: float | None = None
    exhausted_reason: str = ""

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def available(self) -> bool:
        if self.exhausted_until is None:
            return True
        if time.time() >= self.exhausted_until:
            self.exhausted_until, self.exhausted_reason = None, ""
            return True
        return False


@dataclass
class Result:
    agent: str
    ok: bool
    text: str = ""
    status: str = "success"  # success | quota | auth | timeout | cancelled | error
    error: str = ""
    retry_at: float | None = None


OnActivity = Callable[[Activity], Awaitable[None] | None]


# --------------------------------------------------------------------------- commands

def build_command(agent: str, mode: str, cwd: Path, prompt: str, *, model: str = "",
                  executable: str = "", network: bool = True, web: bool = True) -> tuple[list[str], str | None]:
    """Return (argv, stdin_text). mode is "think" (read-only) or "work" (may edit/run)."""
    exe = executable or EXECUTABLES[agent]
    work = mode == "work"
    if agent == "claude":
        tools = "Bash Read Write Edit MultiEdit Glob Grep TodoWrite" if work else "Read Glob Grep"
        if web:
            tools += " WebSearch WebFetch"
        argv = [exe, "-p", "--output-format", "stream-json", "--verbose",
                "--permission-mode", "acceptEdits" if work else "default",
                "--allowedTools", tools, "--no-session-persistence"]
        if not work:
            argv += ["--disallowedTools", "Bash Write Edit MultiEdit NotebookEdit"]
        if model:
            argv += ["--model", model]
        return argv, prompt
    if agent == "codex":
        argv = [exe] + (["--search"] if web else []) + ["exec", "--json", "--skip-git-repo-check", "--color", "never",
                "-s", "workspace-write" if work else "read-only", "-C", str(cwd)]
        if work and network:
            argv += ["-c", "sandbox_workspace_write.network_access=true"]
        if model:
            argv += ["-m", model]
        return argv + ["-"], prompt
    if agent == "antigravity":
        argv = [exe, "--output-format", "stream-json", "--disable-slash-commands"]
        if work:
            argv += ["--mode", "accept-edits",
                     "--settings", json.dumps({"permissions": {"allow": ["command(*)"]}})]
        else:
            # Headless agy aborts a turn on any un-allowed command, so allow only read-only ones.
            # (Its --mode plan writes a plan artifact instead of answering, so it isn't used.)
            argv += ["--settings", json.dumps({"permissions": {"allow": [f"command({c})" for c in READONLY_COMMANDS]}})]
        if model:
            argv += ["--model", model]
        return argv + ["-p", prompt], None
    raise ValueError(f"unknown agent {agent!r}")


# --------------------------------------------------------------------------- parsing

def _short(value: Any, limit: int = 140) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _rel(path: str, cwd: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(cwd.resolve()))
    except (ValueError, OSError):
        return path


def _unwrap_shell(cmd: str) -> str:
    m = re.match(r"^\S*(?:ba|z)?sh -l?c (.*)$", cmd, re.S)
    if m:
        try:
            parts = shlex.split(m.group(1))
            if len(parts) == 1:
                return parts[0]
        except ValueError:
            pass
    return cmd


def describe_command(cmd: str) -> str:
    """Shell command for display; team-chat tool calls become plain language."""
    m = re.search(r"msg\.py\s+--as\s+\S+\s+(send|inbox|wait|log|ask|checkin)\b\s*(\S*)", cmd)
    if m:
        verb, target = m.groups()
        return {"send": f"💬 messages {target.strip(chr(34) + chr(39))}", "inbox": "📥 checks team chat",
                "wait": "⏳ waits for a reply", "log": "📜 reads team chat history",
                "ask": "❓ asks the lead (waiting for answer)", "checkin": "🧭 checks in with the lead"}[verb]
    return "$ " + _short(cmd)


def _describe_tool(name: str, params: dict[str, Any], cwd: Path) -> str:
    params = params if isinstance(params, dict) else {}
    lname = name.lower()
    for key in ("command", "CommandLine", "cmd"):
        if key in params:
            return describe_command(str(params[key]))
    for key in ("file_path", "path", "Path", "TargetFile", "AbsolutePath", "notebook_path"):
        if key in params:
            verb = ("edit" if "edit" in lname or "replace" in lname else
                    "write" if "write" in lname or "create" in lname else "read" if "read" in lname or "view" in lname else lname)
            return f"{verb} {_rel(str(params[key]), cwd)}"
    if lname in ("websearch", "search_web"):
        return "🔎 search " + _short(params.get("query") or params.get("Query") or "", 100)
    if lname in ("webfetch", "read_url_content", "open_browser_url", "read_browser_page"):
        return "🌐 read " + _short(params.get("url") or params.get("Url") or params.get("URL") or "", 100)
    for key in ("pattern", "Query", "query", "SearchPath"):
        if key in params:
            return f"{lname} {_short(params[key], 80)}"
    return lname


class StreamParser:
    """Turns one vendor's JSON-lines stream into Activities, final text and usage."""

    def __init__(self, agent: str, cwd: Path, member: str = ""):
        self.kind, self.agent, self.cwd = provider_of(agent), member or agent, cwd
        self.final_text = ""
        self.texts: list[str] = []
        self.success: bool | None = None
        self.error = ""
        self.input_tokens = self.output_tokens = 0
        self.quota: dict[str, float] = {}
        self.retry_at: float | None = None
        self.rate_limited = False

    def feed(self, line: str) -> list[Activity]:
        line = line.strip()
        if not line.startswith("{"):
            return []
        try:
            event = json.loads(line)
        except ValueError:
            return []
        if not isinstance(event, dict):
            return []
        return getattr(self, "_" + self.kind)(event)

    # Claude Code: --output-format stream-json --verbose
    def _claude(self, e: dict[str, Any]) -> list[Activity]:
        out: list[Activity] = []
        kind = e.get("type")
        if kind == "assistant":
            for block in e.get("message", {}).get("content", []) or []:
                if block.get("type") == "tool_use":
                    out.append(Activity(self.agent, "tool", _describe_tool(block.get("name", "tool"), block.get("input", {}), self.cwd)))
                elif block.get("type") == "text" and block.get("text", "").strip():
                    self.texts.append(block["text"])
                    out.append(Activity(self.agent, "text", block["text"]))
        elif kind == "rate_limit_event":
            info = e.get("rate_limit_info", {}) or {}
            for name, window in (info.get("unifiedWindows") or {}).items():
                if isinstance(window, dict) and "utilization" in window:
                    label = {"five_hour": "5h", "seven_day": "7d"}.get(name, name)
                    self.quota[label] = float(window["utilization"])
            if info.get("status") == "rejected":
                self.rate_limited = True
                if info.get("resetsAt"):
                    self.retry_at = float(info["resetsAt"])
        elif kind == "result":
            self.success = e.get("subtype") == "success" and not e.get("is_error")
            self.final_text = str(e.get("result") or "")
            usage = e.get("usage") or {}
            self.input_tokens += int(usage.get("input_tokens", 0) or 0) + int(usage.get("cache_creation_input_tokens", 0) or 0)
            self.output_tokens += int(usage.get("output_tokens", 0) or 0)
            if not self.success:
                self.error = _short(e.get("errors") or e.get("result") or e.get("subtype"), 2000)
        return out

    # Codex: codex exec --json
    def _codex(self, e: dict[str, Any]) -> list[Activity]:
        out: list[Activity] = []
        kind = e.get("type")
        item = e.get("item") or {}
        itype = item.get("type")
        if kind == "item.completed" and itype == "web_search":
            query = (item.get("action") or {}).get("query") or item.get("query") or ""
            if query:
                out.append(Activity(self.agent, "tool", f"🔎 search {_short(query, 100)}"))
        if kind == "item.started" and itype == "command_execution":
            out.append(Activity(self.agent, "tool", describe_command(_unwrap_shell(item.get("command", "")))))
        elif kind == "item.completed":
            if itype == "agent_message" and str(item.get("text", "")).strip():
                self.texts.append(item["text"])
                self.final_text = item["text"]
                out.append(Activity(self.agent, "text", item["text"]))
            elif itype == "file_change":
                for change in item.get("changes", []) or []:
                    out.append(Activity(self.agent, "tool", f"{change.get('kind', 'edit')} {_rel(change.get('path', ''), self.cwd)}"))
            elif itype == "command_execution" and item.get("exit_code") not in (0, None):
                out.append(Activity(self.agent, "info", f"exit {item.get('exit_code')}"))
        elif kind == "turn.completed":
            self.success = True
            usage = e.get("usage") or {}
            # codex's input_tokens includes cached tokens; count only the fresh part
            self.input_tokens += max(0, int(usage.get("input_tokens", 0) or 0) - int(usage.get("cached_input_tokens", 0) or 0))
            self.output_tokens += int(usage.get("output_tokens", 0) or 0)
        elif kind in ("turn.failed", "error"):
            err = e.get("error", e)
            self.error = _short(err.get("message", err) if isinstance(err, dict) else err, 2000)
            if kind == "turn.failed":
                self.success = False
        return out

    # Antigravity: agy --output-format stream-json
    def _antigravity(self, e: dict[str, Any]) -> list[Activity]:
        out: list[Activity] = []
        kind = e.get("event")
        if kind == "step_update":
            step = e.get("step_update") or {}
            if step.get("step_type") == "tool" and step.get("state") == "ACTIVE":
                info = step.get("tool_info") or {}
                out.append(Activity(self.agent, "tool", _describe_tool(step.get("tool_name") or info.get("name", "tool"), info.get("parameters", {}), self.cwd)))
        elif kind == "result":
            result = e.get("result") or {}
            self.success = result.get("status") == "SUCCESS"
            self.final_text = str(result.get("response") or "")
            if self.final_text.strip():
                self.texts.append(self.final_text)
            usage = result.get("usage") or {}
            # agy's input_tokens includes cache reads; count only the fresh part
            self.input_tokens += max(0, int(usage.get("input_tokens", 0) or 0) - int(usage.get("cache_read_tokens", 0) or 0))
            self.output_tokens += int(usage.get("output_tokens", 0) or 0) + int(usage.get("thinking_tokens", 0) or 0)
            if result.get("denied_actions"):
                out.append(Activity(self.agent, "info", f"{len(result['denied_actions'])} action(s) were denied by the CLI"))
            if not self.success:
                self.error = _short(result.get("error") or result.get("status"), 2000)
        elif kind == "error":
            self.error = _short(e.get("error") or e, 2000)
        return out


# --------------------------------------------------------------------------- errors

def classify_error(text: str) -> str:
    lower = text.lower()
    if re.search(r"\b(429|resource_exhausted|rate_limit(?:_exceeded)?|usage_limit_reached)\b|rate limit|quota (?:exceeded|exhausted)|usage limit|hit your limit|too many requests|credits exhausted|out of (?:usage|credits)", lower):
        return "quota"
    if re.search(r"authentication required|not (?:logged|signed) in|invalid (?:api key|authentication|token)|unauthori[sz]ed|token expired|please (?:log|sign) ?in|\b401\b", lower):
        return "auth"
    return "error"


def retry_hint(text: str) -> float | None:
    m = re.search(r"(?i)(?:retry[- ]after|try again in|retry in)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)?\b", text)
    if not m:
        return None
    unit = (m.group(2) or "s").lower()
    return time.time() + float(m.group(1)) * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)


# --------------------------------------------------------------------------- running

def installed(agent: str, executable: str = "") -> str | None:
    return shutil.which(executable or EXECUTABLES[provider_of(agent)])


async def run_agent(agent: str, mode: str, cwd: Path, prompt: str, *, usage: Usage,
                    on_activity: OnActivity | None = None, model: str = "", executable: str = "",
                    timeout: float = 1800, env: dict[str, str] | None = None,
                    argv_prefix: list[str] | None = None, network: bool = True, web: bool = True,
                    member_usage: Usage | None = None) -> Result:
    """Run one turn for a member (claude, codex-2, ...) to completion, streaming activity as it happens."""
    provider = provider_of(agent)
    argv, stdin_text = build_command(provider, mode, cwd, prompt, model=model, executable=executable, network=network, web=web)
    if argv_prefix:  # demo mode: swap the vendor binary for the offline stand-in
        argv = argv_prefix + argv[1:]
    parser = StreamParser(provider, cwd, member=agent)
    started = time.time()

    async def emit(activity: Activity) -> None:
        if on_activity:
            maybe = on_activity(activity)
            if asyncio.iscoroutine(maybe):
                await maybe

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd), env={**os.environ, **(env or {})},
            stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=32 * 1024 * 1024, start_new_session=True)
    except FileNotFoundError:
        return Result(agent, False, status="error", error=f"{argv[0]} is not installed")

    stderr_chunks: list[bytes] = []

    async def pump_stdout() -> None:
        assert proc.stdout
        async for raw in proc.stdout:
            for act in parser.feed(raw.decode("utf-8", "replace")):
                await emit(act)

    async def pump_stderr() -> None:
        assert proc.stderr
        async for raw in proc.stderr:
            stderr_chunks.append(raw)

    async def feed_stdin() -> None:
        if stdin_text is not None and proc.stdin:
            try:
                proc.stdin.write(stdin_text.encode())
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()

    status = ""
    try:
        await asyncio.wait_for(asyncio.gather(feed_stdin(), pump_stdout(), pump_stderr(), proc.wait()), timeout)
    except asyncio.TimeoutError:
        status = "timeout"
    except asyncio.CancelledError:
        _kill(proc)
        raise
    finally:
        if proc.returncode is None:
            _kill(proc)
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                pass

    stderr = b"".join(stderr_chunks).decode("utf-8", "replace")[-6000:]
    for u in (usage, member_usage):  # the subscription total and, if given, this member's own tally
        if u is not None:
            u.calls += 1
            u.seconds += time.time() - started
            u.input_tokens += parser.input_tokens
            u.output_tokens += parser.output_tokens
    usage.quota.update(parser.quota)

    text = parser.final_text.strip() or (parser.texts[-1].strip() if parser.texts else "")
    if status == "timeout":
        return Result(agent, False, text, "timeout", f"no result after {int(timeout)}s")
    if parser.success and proc.returncode == 0 and text:
        return Result(agent, True, text)

    diagnostic = "\n".join(x for x in (parser.error, stderr) if x).strip() or f"{argv[0]} exited with code {proc.returncode} and no result"
    kind = "quota" if parser.rate_limited else classify_error(diagnostic)
    retry_at = parser.retry_at or retry_hint(diagnostic)
    if kind == "quota":
        usage.exhausted_until = retry_at or time.time() + 3600
        usage.exhausted_reason = "usage limit reached"
    elif kind == "auth":
        usage.exhausted_until = time.time() + 365 * 86400
        usage.exhausted_reason = "not signed in"
    return Result(agent, False, text, kind, diagnostic[-3000:], retry_at)


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def demo_prefix() -> list[str]:
    return [sys.executable, str(Path(__file__).with_name("fake_agent.py"))]
