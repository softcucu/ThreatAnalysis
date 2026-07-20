"""Skill helpers for agent runtime integrations."""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path


_INSTALL_LOCK = threading.Lock()


def skill_name_from_path(skill_path: str | Path) -> str:
    """Return the OpenCode skill name implied by a skill path."""
    path = Path(skill_path)
    if path.name == "SKILL.md":
        return path.parent.name
    if path.suffix.lower() in {".md", ".markdown", ".txt"}:
        return path.stem
    return path.name


def install_opencode_skill(skill_path: str | Path, directory: str | Path) -> Path:
    """Install a skill and configure OpenCode to load the runtime skills dir."""
    source = Path(skill_path).expanduser().resolve()
    skill_name = skill_name_from_path(source)
    workspace = Path(directory).expanduser().resolve()
    if not workspace.is_dir():
        raise NotADirectoryError(f"OpenCode directory does not exist: {workspace}")
    target = workspace / ".opencode" / "skills" / skill_name

    with _INSTALL_LOCK:
        if source.is_dir():
            skill_file = source / "SKILL.md"
            if not skill_file.is_file():
                raise FileNotFoundError(f"Skill file does not exist: {skill_file}")
            if _same_path(source, target):
                _ensure_opencode_skills_config(workspace, target.parent)
                return target
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target, dirs_exist_ok=True)
            _ensure_opencode_skills_config(workspace, target.parent)
            return target

        if not source.is_file():
            raise FileNotFoundError(f"Skill file does not exist: {source}")

        source_root = source.parent if source.name == "SKILL.md" else None
        if source_root is not None and not _same_path(source_root, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_root, target, dirs_exist_ok=True)
            _ensure_opencode_skills_config(workspace, target.parent)
            return target

        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        _ensure_opencode_skills_config(workspace, target.parent)
        return target


def _ensure_opencode_skills_config(workspace: Path, skills_dir: Path) -> Path:
    config_path = workspace / "opencode.json"
    if config_path.is_file():
        data = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"OpenCode config must be a JSON object: {config_path}")
    else:
        data = {"$schema": "https://opencode.ai/config.json"}

    skills = data.get("skills")
    if not isinstance(skills, dict):
        skills = {}
        data["skills"] = skills

    runtime_path = str(skills_dir.resolve())
    existing_paths = skills.get("paths")
    paths = [str(path) for path in existing_paths] if isinstance(existing_paths, list) else []
    skills["paths"] = [runtime_path] + [path for path in paths if path != runtime_path]

    config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_path


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except FileNotFoundError:
        return False
