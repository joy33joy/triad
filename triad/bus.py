"""The team chat: a shared, append-only message board inside the workspace.

Agents talk to each other *while they work* by running a tiny stdlib-only
script that Triad drops into ``.triad/msg.py``. Every CLI can run shell
commands, so this works the same way for Claude, Codex and Antigravity.
Triad tails the board and shows the conversation live; the user can post too.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

MSG_TOOL = r'''#!/usr/bin/env python3
"""Triad team chat. Usage (run from the project root):
  python3 .triad/msg.py --as ME send TO "message"     TO = a teammate or "all"
  python3 .triad/msg.py --as ME ask "question"        ask the LEAD; waits for the answer (about a minute)
  python3 .triad/msg.py --as ME checkin "approach"    tell the lead your plan of attack (once, early)
  python3 .triad/msg.py --as ME inbox                 show new messages for you
  python3 .triad/msg.py --as ME wait [--timeout 120]  block until a new message arrives
  python3 .triad/msg.py --as ME log                   whole team conversation
"""
import argparse, fcntl, json, os, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOARD = HERE / "board.jsonl"
_INFO = json.loads((HERE / "team.json").read_text()) if (HERE / "team.json").exists() else {}
TEAM = _INFO.get("team", []) if isinstance(_INFO, dict) else _INFO

def load():
    if not BOARD.exists():
        return []
    out = []
    for line in BOARD.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out

def post(me, to, text, kind="chat"):
    BOARD.parent.mkdir(parents=True, exist_ok=True)
    with open(BOARD, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        n = sum(1 for _ in f) + 1
        f.write(json.dumps({"id": n, "ts": time.time(), "from": me, "to": to, "text": text, "kind": kind}) + "\n")
    return n

def mine(msgs, me):
    return [m for m in msgs if m.get("from") != me and m.get("to") in (me, "all")]

def cursor_path(me):
    return HERE / "cursors" / me

def read_cursor(me):
    try:
        return int(cursor_path(me).read_text())
    except (OSError, ValueError):
        return 0

def show(msgs):
    for m in msgs:
        print(f"[#{m['id']}] {m['from']} -> {m['to']}: {m['text']}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--as", dest="me", default=os.environ.get("TRIAD_AGENT", ""))
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send"); s.add_argument("to"); s.add_argument("text", nargs="+")
    q = sub.add_parser("ask"); q.add_argument("text", nargs="+"); q.add_argument("--timeout", type=float, default=240)
    c = sub.add_parser("checkin"); c.add_argument("text", nargs="+")
    sub.add_parser("inbox"); sub.add_parser("log")
    w = sub.add_parser("wait"); w.add_argument("--timeout", type=float, default=120)
    a = ap.parse_args()
    if not a.me:
        sys.exit("say who you are: --as <your name>")
    if a.cmd == "send":
        to = a.to.lower()
        if TEAM and to not in TEAM + ["all", "lead", "user"]:
            sys.exit(f"unknown recipient {to!r}; choose from {', '.join(TEAM + ['all', 'lead'])}")
        kind = "question" if to == "lead" else "chat"
        print(f"sent #{post(a.me, to, ' '.join(a.text), kind)} to {to}")
        return
    if a.cmd == "checkin":
        print(f"checked in #{post(a.me, 'lead', ' '.join(a.text), 'checkin')}; keep working")
        return
    if a.cmd == "ask":
        n = post(a.me, "lead", " ".join(a.text), "question")
        deadline = time.time() + a.timeout
        while time.time() < deadline:
            replies = [m for m in load() if m["id"] > n and m.get("from") == "lead" and m.get("to") in (a.me, "all")]
            if replies:
                show(replies)
                return
            time.sleep(0.5)
        print("(the lead has not answered yet; make the most sensible assumption, note it in your handoff, and continue)")
        return
    if a.cmd == "log":
        show(load()); return
    deadline = time.time() + (a.timeout if a.cmd == "wait" else 0)
    while True:
        new = [m for m in mine(load(), a.me) if m["id"] > read_cursor(a.me)]
        if new or time.time() >= deadline:
            break
        time.sleep(1)
    if new:
        show(new)
        cursor_path(a.me).parent.mkdir(exist_ok=True)
        cursor_path(a.me).write_text(str(max(m["id"] for m in new)))
    else:
        print("(no new messages)")

if __name__ == "__main__":
    main()
'''


class Board:
    def __init__(self, workspace: Path, team: list[str], lead: str = ""):
        self.dir = workspace / ".triad"
        self.path = self.dir / "board.jsonl"
        self.team = team
        self.lead = lead
        self._seen = 0

    def install(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "msg.py").write_text(MSG_TOOL, encoding="utf-8")
        (self.dir / "msg.py").chmod(0o755)
        (self.dir / "team.json").write_text(json.dumps({"team": self.team, "lead": self.lead}))
        (self.dir / "cursors").mkdir(exist_ok=True)
        self.path.touch()
        self._seen = len(self.all())

    def set_team(self, team: list[str]) -> None:
        self.team = list(team)
        if self.dir.exists():
            (self.dir / "team.json").write_text(json.dumps({"team": self.team, "lead": self.lead}))

    def reset(self) -> None:
        """Start a fresh conversation for a new project in the same workspace."""
        if self.path.exists():
            archive = self.dir / f"board-{int(time.time())}.jsonl"
            if self.path.stat().st_size:
                self.path.rename(archive)
        for cursor in (self.dir / "cursors").glob("*"):
            cursor.unlink()
        self.path.touch()
        self._seen = 0

    def all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def post(self, sender: str, to: str, text: str, kind: str = "chat") -> None:
        import fcntl
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            n = sum(1 for _ in f) + 1
            f.write(json.dumps({"id": n, "ts": time.time(), "from": sender, "to": to, "text": text, "kind": kind}) + "\n")

    def poll(self) -> list[dict[str, Any]]:
        """New messages since the last poll."""
        msgs = self.all()
        new, self._seen = msgs[self._seen:], len(msgs)
        return new

    def transcript(self, limit: int = 40) -> str:
        msgs = self.all()[-limit:]
        return "\n".join(f"{m['from']} -> {m['to']}: {m['text'][:600]}" for m in msgs) or "(no messages yet)"
