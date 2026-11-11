"""Skills: reusable know-how the lead can attach to any stage.

A skill is a Markdown file whose first line says what it is for. Built-in skills ship in triad/skills/;
add your own in ~/.config/triad/skills/ (or the folder set by `triad config skills_dir ...`).
A user skill with the same name as a built-in one replaces it.
"""
from __future__ import annotations

from pathlib import Path

from .config import Config, config_path


def skill_dirs(cfg: Config) -> list[Path]:
    dirs = [Path(__file__).with_name("skills"), config_path().parent / "skills"]
    if cfg.skills_dir:
        dirs.append(Path(cfg.skills_dir).expanduser())
    return dirs


def load_skills(cfg: Config) -> dict[str, str]:
    skills: dict[str, str] = {}
    for folder in skill_dirs(cfg):
        if folder.is_dir():
            for f in sorted(folder.glob("*.md")):
                text = f.read_text(encoding="utf-8").strip()
                if text:
                    skills[f.stem] = text[:6000]
    return skills
