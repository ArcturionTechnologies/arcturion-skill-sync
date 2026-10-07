#!/usr/bin/env python3
"""ArcturionSkillSync: project one manifest-defined skill loadout into every harness.

One JSON manifest says which skills each agent gets. This tool symlinks those
skills into Claude Code (`.claude/skills`), Codex and other open-agent harnesses
(`.agents/skills`), and Hermes (`skills.external_dirs` in its config), validates
every SKILL.md frontmatter, and records what it owns so it never deletes a file
it did not create.

Configuration (environment variables, all optional):
  ARC_ROOT                 base directory for legacy-schema agent homes
                           (default: $HOME); a home is ARC_ROOT/<agent>
  HERMES_REPO              Hermes checkout used by --smoke runtime checks
                           (default: ~/.hermes/hermes-agent)
  SKILL_SYNC_CODEX_PROBE   prompt token passed to `codex debug prompt-input`
                           by --codex-smoke (default: $skill-sync-probe)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # Fail closed in _validate_sources with an actionable finding.
    yaml = None


STATE_FILE = ".arcturion-skill-sync.json"
ADAPTERS = {
    "claude": Path(".claude/skills"),
    "open_agent": Path(".agents/skills"),
}
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ALLOWED_FRONTMATTER_KEYS = {"name", "description", "license", "allowed-tools", "metadata", "trigger"}
HERMES_REPO = Path(os.environ.get("HERMES_REPO") or "~/.hermes/hermes-agent").expanduser()
CODEX_PROBE = os.environ.get("SKILL_SYNC_CODEX_PROBE") or "$skill-sync-probe"


def _arc_root() -> Path:
    return Path(os.environ.get("ARC_ROOT") or Path.home()).expanduser()


def hermes_python() -> str:
    """Hermes's own interpreter; its deps (e.g. ruamel since 0.21.5) are absent from system python."""
    for rel in (".venv/bin/python", "venv/bin/python"):
        candidate = HERMES_REPO / rel
        if candidate.exists():
            return str(candidate)
    return sys.executable
HERMES_RUNTIME_MARKER = "__ARCTURION_HERMES_RUNTIME__"


def finding(code: str, agent: str, adapter: str, skill: str, detail: str) -> dict[str, str]:
    return {
        "code": code,
        "agent": agent,
        "adapter": adapter,
        "skill": skill,
        "detail": detail,
    }


def _expand_path(value: str, base: Path) -> Path:
    expanded = Path(os.path.expandvars(os.path.expanduser(value)))
    return expanded if expanded.is_absolute() else base / expanded


def _parse_frontmatter(text: str) -> tuple[dict[str, Any] | None, str | None]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, "SKILL.md must start with YAML frontmatter"
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None, "SKILL.md frontmatter is not closed"
    try:
        data = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        return None, f"malformed YAML frontmatter: {exc}"
    if not isinstance(data, dict):
        return None, "frontmatter must be a YAML mapping"
    return data, None


def _assignment_map(
    values: Any,
    issues: list[dict[str, str]],
    agent: str,
    label: str,
) -> dict[str, str]:
    if not isinstance(values, list):
        issues.append(finding("invalid_manifest", agent, "manifest", "", f"{label} must be a list"))
        return {}
    result: dict[str, str] = {}
    for assignment in values:
        if not isinstance(assignment, dict) or not isinstance(assignment.get("name"), str) or not isinstance(assignment.get("source"), str):
            issues.append(finding("invalid_manifest", agent, "manifest", "", f"each {label} entry requires string name and source"))
            continue
        name = assignment["name"]
        if name in result:
            issues.append(finding("invalid_manifest", agent, "manifest", name, f"duplicate {label} name"))
            continue
        result[name] = assignment["source"]
    return result


def _skills_hash(skills: dict[str, Path]) -> str:
    payload = [(name, str(source.resolve())) for name, source in sorted(skills.items())]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()


def _load_manifest(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, Path] | None]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [finding("invalid_manifest", "UNIVERSE", "manifest", "", str(exc))], None
    schema_version = raw.get("schema_version", raw.get("version"))
    if schema_version not in {1, 2} or not isinstance(raw.get("agents"), dict):
        return [], [finding("invalid_manifest", "UNIVERSE", "manifest", "", "requires schema version 1 or 2 and agents object")], None

    agents: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    baseline: dict[str, Path] | None = None
    if schema_version == 2:
        baseline_values = _assignment_map(raw.get("universe_baseline"), issues, "UNIVERSE", "universe_baseline")
        if not baseline_values:
            issues.append(finding("invalid_manifest", "UNIVERSE", "manifest", "", "universe_baseline must not be empty"))
        baseline = {
            name: _expand_path(source, path.parent).resolve()
            for name, source in sorted(baseline_values.items())
        }
    for agent, config in sorted(raw["agents"].items()):
        if not isinstance(config, dict):
            issues.append(finding("invalid_manifest", agent, "manifest", "", "agent configuration must be an object"))
            continue
        canonical = "assignments" in config or "root" in config
        if canonical:
            root_value = config.get("root")
            if not isinstance(root_value, str):
                issues.append(finding("invalid_manifest", agent, "manifest", "", "agent requires a root"))
                continue
            root = _expand_path(root_value, path.parent).resolve()
            home_value = str(root.parent.parent)
            if baseline is not None:
                if config.get("inherits_universe_baseline") is not True:
                    issues.append(finding("missing_universe_baseline_inheritance", agent, "manifest", "", "schema version 2 agents must set inherits_universe_baseline=true"))
                if config.get("assignments"):
                    issues.append(finding("agent_specific_assignment_forbidden", agent, "manifest", "", "schema version 2 agents inherit the complete universe_baseline"))
                skill_values = {name: str(source) for name, source in baseline.items()}
            else:
                skill_values = _assignment_map(config.get("assignments"), issues, agent, "assignments")
        else:
            if not isinstance(config.get("skills"), dict):
                issues.append(finding("invalid_manifest", agent, "manifest", "", "agent requires a skills object"))
                continue
            home_value = config.get("home", str(_arc_root() / agent))
            skill_values = config["skills"]
        home = _expand_path(str(home_value), path.parent).resolve()
        skills: dict[str, Path] = {}
        for name, source_value in sorted(skill_values.items()):
            if not isinstance(name, str) or not name or not isinstance(source_value, str):
                issues.append(finding("invalid_manifest", agent, "manifest", str(name), "skill names and sources must be strings"))
                continue
            skills[name] = _expand_path(source_value, path.parent).resolve()
        overrides: list[dict[str, Any]] = []
        raw_overrides = config.get("hermes_overrides", [])
        if not isinstance(raw_overrides, list):
            issues.append(finding("invalid_manifest", agent, "manifest", "", "hermes_overrides must be a list"))
        else:
            for override in raw_overrides:
                if not isinstance(override, dict) or not isinstance(override.get("name"), str) or not isinstance(override.get("local_path"), str):
                    issues.append(finding("invalid_manifest", agent, "manifest", "", "each hermes override requires string name and local_path"))
                    continue
                override_name = override["name"]
                if override_name not in skills:
                    issues.append(finding("invalid_manifest", agent, "manifest", override_name, "Hermes override has no matching assignment"))
                    continue
                local_path = _expand_path(override["local_path"], path.parent)
                overrides.append({"name": override_name, "local_path": Path(os.path.abspath(local_path)), "source": skills[override_name]})
        hermes = config.get("hermes_config")
        agents.append({
            "name": agent,
            "home": home,
            "skills": skills,
            "hermes_config": _expand_path(hermes, path.parent).resolve() if isinstance(hermes, str) else None,
            "hermes_overrides": overrides,
        })
    return agents, issues, baseline


def _validate_sources(agents: list[dict[str, Any]]) -> list[dict[str, str]]:
    if yaml is None:
        return [finding(
            "validator_dependency_missing",
            "UNIVERSE",
            "source",
            "",
            "PyYAML is required for strict SKILL.md frontmatter validation",
        )]
    issues: list[dict[str, str]] = []
    for agent in agents:
        for name, source in agent["skills"].items():
            skill_md = source / "SKILL.md"
            if not source.is_dir() or not skill_md.is_file():
                issues.append(finding("missing_skill", agent["name"], "source", name, f"missing {skill_md}"))
                continue
            text = skill_md.read_text(encoding="utf-8", errors="replace")
            frontmatter, detail = _parse_frontmatter(text)
            if detail is None:
                unexpected = set(frontmatter) - ALLOWED_FRONTMATTER_KEYS
                frontmatter_name = frontmatter.get("name")
                description = frontmatter.get("description")
                metadata = frontmatter.get("metadata")
                allowed_tools = frontmatter.get("allowed-tools")
                trigger = frontmatter.get("trigger")
                if unexpected:
                    detail = "unexpected frontmatter key(s): " + ", ".join(sorted(map(str, unexpected)))
                elif metadata is not None and (
                    not isinstance(metadata, dict)
                    or any(not isinstance(key, str) or not isinstance(value, str) for key, value in metadata.items())
                ):
                    detail = "frontmatter metadata must be a mapping with string keys and string values"
                elif "allowed-tools" in frontmatter and not isinstance(allowed_tools, str):
                    detail = "frontmatter allowed-tools must be a string"
                elif "trigger" in frontmatter and not isinstance(trigger, str):
                    detail = "frontmatter trigger must be a string"
                elif not isinstance(frontmatter_name, str) or not frontmatter_name:
                    detail = "frontmatter name must be a non-empty string"
                elif len(frontmatter_name) > 64:
                    detail = "frontmatter name must be at most 64 characters"
                elif not SKILL_NAME_RE.fullmatch(frontmatter_name):
                    detail = f"frontmatter name {frontmatter_name!r} must match {SKILL_NAME_RE.pattern}"
                elif not isinstance(description, str) or not description.strip():
                    detail = "frontmatter description must be a non-empty string"
                elif len(description) > 1024:
                    detail = "frontmatter description must be at most 1024 characters"
                elif "<" in description or ">" in description:
                    detail = "frontmatter description must not contain angle brackets"
                else:
                    continue
            issues.append(finding("invalid_frontmatter", agent["name"], "source", name, detail))
    return issues


def _load_state(directory: Path, agent: str, adapter: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    state_path = directory / STATE_FILE
    if not state_path.exists():
        return {}, []
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        links = state.get("links", {})
        if state.get("version") != 1 or not isinstance(links, dict):
            raise ValueError("requires version=1 and links object")
        return {str(k): str(v) for k, v in links.items()}, []
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {}, [finding("invalid_state", agent, adapter, "", str(exc))]


def _link_target(path: Path) -> Path:
    target = Path(os.readlink(path))
    return (path.parent / target).resolve() if not target.is_absolute() else target.resolve()


def _project_adapter(agent: dict[str, Any], adapter: str, apply: bool) -> tuple[list[dict[str, str]], int]:
    directory = agent["home"] / ADAPTERS[adapter]
    managed, issues = _load_state(directory, agent["name"], adapter)
    if issues:
        return issues, 0
    desired: dict[str, Path] = agent["skills"]
    next_managed: dict[str, str] = {}
    changes = 0

    for name, source in desired.items():
        destination = directory / name
        source_resolved = source.resolve()
        if destination.is_symlink():
            current = _link_target(destination)
            if current == source_resolved:
                if name in managed:
                    next_managed[name] = str(source_resolved)
                continue
            if name not in managed:
                issues.append(finding("unowned_conflict", agent["name"], adapter, name, f"unowned symlink targets {current}"))
                continue
            if apply:
                destination.unlink()
                destination.symlink_to(source_resolved)
                changes += 1
                next_managed[name] = str(source_resolved)
            else:
                issues.append(finding("wrong_target", agent["name"], adapter, name, f"expected {source_resolved}, found {current}"))
            continue

        if destination.exists():
            if destination.resolve() != source_resolved:
                issues.append(finding("unowned_conflict", agent["name"], adapter, name, "real file or directory occupies projection path"))
            continue

        if apply:
            directory.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(source_resolved)
            changes += 1
            next_managed[name] = str(source_resolved)
        else:
            issues.append(finding("missing_projection", agent["name"], adapter, name, str(destination)))

    for name, old_source in managed.items():
        if name in desired:
            continue
        destination = directory / name
        if destination.is_symlink() and _link_target(destination) == Path(old_source).resolve():
            if apply:
                destination.unlink()
                changes += 1
            else:
                issues.append(finding("stale_projection", agent["name"], adapter, name, str(destination)))
        elif destination.exists() or destination.is_symlink():
            issues.append(finding("stale_owned_conflict", agent["name"], adapter, name, "owned path changed outside projector; left untouched"))

    if directory.is_dir():
        for entry in sorted(directory.iterdir(), key=lambda item: item.name):
            if entry.name in desired or entry.name.startswith((".", "_")):
                continue
            if entry.is_dir() and (entry / "SKILL.md").is_file():
                issues.append(finding(
                    "unmanifested_projection",
                    agent["name"],
                    adapter,
                    entry.name,
                    f"usable skill is absent from manifest and was left untouched: {entry}",
                ))

    if apply and (directory.exists() or next_managed):
        directory.mkdir(parents=True, exist_ok=True)
        state_path = directory / STATE_FILE
        state_text = json.dumps({"version": 1, "adapter": adapter, "links": next_managed}, indent=2, sort_keys=True) + "\n"
        if not state_path.exists() or state_path.read_text(encoding="utf-8") != state_text:
            state_path.write_text(state_text, encoding="utf-8")
            changes += 1
    return issues, changes


def _hermes_external_dirs(text: str) -> list[str]:
    if yaml is None:
        raise ValueError("PyYAML is required to inspect Hermes configuration")
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid Hermes YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Hermes configuration must be a YAML mapping")
    skills = data.get("skills") or {}
    if not isinstance(skills, dict):
        raise ValueError("Hermes skills configuration must be a mapping")
    external = skills.get("external_dirs") or []
    if isinstance(external, str):
        external = [external]
    if not isinstance(external, list) or any(not isinstance(item, str) for item in external):
        raise ValueError("Hermes skills.external_dirs must be a list of paths")
    return external


def ensure_hermes_external_dir(config: Path, skill_dir: Path, apply: bool, agent: str) -> tuple[list[dict[str, str]], int]:
    if not config.is_file():
        return [finding("missing_hermes_config", agent, "hermes", "", str(config))], 0
    text = config.read_text(encoding="utf-8")
    wanted = str(skill_dir.resolve())
    try:
        current_external_dirs = _hermes_external_dirs(text)
    except ValueError as exc:
        return [finding("invalid_hermes_config", agent, "hermes", "", str(exc))], 0
    if wanted in current_external_dirs:
        return [], 0
    if not apply:
        return [finding("missing_hermes_projection", agent, "hermes", "", wanted)], 0

    lines = text.splitlines()
    skills_index = next((i for i, line in enumerate(lines) if re.match(r"^skills:\s*$", line)), None)
    if skills_index is None:
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend(["skills:", "  external_dirs:", f"    - {wanted}"])
    else:
        block_end = next((i for i in range(skills_index + 1, len(lines)) if lines[i] and not lines[i][0].isspace()), len(lines))
        external_index = next((i for i in range(skills_index + 1, block_end) if re.match(r"^  external_dirs:", lines[i])), None)
        if external_index is None:
            lines[skills_index + 1:skills_index + 1] = ["  external_dirs:", f"    - {wanted}"]
        else:
            value = lines[external_index].split(":", 1)[1].strip()
            if value == "[]" or value == "":
                lines[external_index] = "  external_dirs:"
                lines.insert(external_index + 1, f"    - {wanted}")
            elif value.startswith("[") and value.endswith("]"):
                lines[external_index:external_index + 1] = ["  external_dirs:"] + [f"    - {entry}" for entry in current_external_dirs + [wanted]]
            else:
                lines[external_index:external_index + 1] = ["  external_dirs:", f"    - {value.strip(chr(39) + chr(34))}", f"    - {wanted}"]
                config.write_text("\n".join(lines) + "\n", encoding="utf-8")
                return [], 1
            insert_at = external_index + 1
            while insert_at < len(lines) and re.match(r"^    -\s+", lines[insert_at]):
                insert_at += 1
            if not any(line == f"    - {wanted}" for line in lines[external_index + 1:insert_at]):
                lines.insert(insert_at, f"    - {wanted}")
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [], 1


def _hermes_backup_path(local_path: Path) -> Path | None:
    skills_root = next((parent for parent in local_path.parents if parent.name == "skills"), None)
    if skills_root is None:
        return None
    return skills_root.parent / ".arcturion-shadowed-skills" / local_path.relative_to(skills_root)


def apply_hermes_override(override: dict[str, Any], apply: bool, agent: str) -> tuple[list[dict[str, str]], int]:
    name = override["name"]
    local_path: Path = override["local_path"]
    source: Path = override["source"]
    source_resolved = source.resolve()

    if local_path.is_symlink():
        current = _link_target(local_path)
        if current == source_resolved:
            return [], 0
        return [finding("wrong_hermes_override", agent, "hermes", name, f"expected {source_resolved}, found {current}; left untouched")], 0

    if not local_path.exists():
        return [finding("missing_hermes_override", agent, "hermes", name, str(local_path))], 0
    if not local_path.is_dir():
        return [finding("hermes_override_conflict", agent, "hermes", name, "local_path is not a directory; left untouched")], 0
    if local_path.resolve() == source_resolved:
        return [], 0
    if not apply:
        return [finding("hermes_override_not_applied", agent, "hermes", name, str(local_path))], 0

    backup = _hermes_backup_path(local_path)
    if backup is None:
        return [finding("invalid_hermes_override_path", agent, "hermes", name, "local_path must be under a profile skills directory")], 0
    if backup.exists() or backup.is_symlink():
        return [finding("hermes_backup_conflict", agent, "hermes", name, f"backup already exists at {backup}; left untouched")], 0

    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(local_path), str(backup))
    local_path.symlink_to(source_resolved)
    return [], 1


def _run_hermes_runtime(hermes_home: Path, name: str) -> dict[str, Any]:
    script = (
        "import json, sys\n"
        "from agent.prompt_builder import build_skills_system_prompt\n"
        "from tools.skills_tool import skill_view\n"
        "result = {'prompt': build_skills_system_prompt(), "
        "'view': json.loads(skill_view(sys.argv[1], preprocess=False))}\n"
        f"print({HERMES_RUNTIME_MARKER!r} + json.dumps(result))\n"
    )
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    old_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(HERMES_REPO) + (os.pathsep + old_pythonpath if old_pythonpath else "")
    completed = subprocess.run(
        [hermes_python(), "-c", script, name],
        cwd=HERMES_REPO,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise RuntimeError(detail)
    marked = next(
        (line[len(HERMES_RUNTIME_MARKER):] for line in reversed(completed.stdout.splitlines()) if line.startswith(HERMES_RUNTIME_MARKER)),
        None,
    )
    if marked is None:
        raise RuntimeError("Hermes runtime returned no machine-readable result")
    try:
        result = json.loads(marked)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Hermes runtime returned invalid JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Hermes runtime result must be an object")
    return result


def _run_hermes_surface_runtime(hermes_home: Path, names: list[str]) -> dict[str, Any]:
    script = (
        "import json, sys\n"
        "from agent.prompt_builder import build_skills_system_prompt\n"
        "from tools.skills_tool import skill_view\n"
        "names = json.loads(sys.argv[1])\n"
        "result = {'prompt': build_skills_system_prompt(), 'views': {name: json.loads(skill_view(name, preprocess=False)) for name in names}}\n"
        f"print({HERMES_RUNTIME_MARKER!r} + json.dumps(result))\n"
    )
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    old_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(HERMES_REPO) + (os.pathsep + old_pythonpath if old_pythonpath else "")
    completed = subprocess.run(
        [hermes_python(), "-c", script, json.dumps(names)],
        cwd=HERMES_REPO,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise RuntimeError(detail)
    marked = next(
        (line[len(HERMES_RUNTIME_MARKER):] for line in reversed(completed.stdout.splitlines()) if line.startswith(HERMES_RUNTIME_MARKER)),
        None,
    )
    if marked is None:
        raise RuntimeError("Hermes runtime returned no machine-readable result")
    try:
        result = json.loads(marked)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Hermes runtime returned invalid JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Hermes runtime result must be an object")
    return result


def validate_hermes_runtime(
    agent: dict[str, Any],
    override: dict[str, Any],
    runner: Any = None,
) -> tuple[list[dict[str, str]], dict[str, str] | None]:
    agent_name = agent["name"]
    skill_alias = override["name"]
    config = agent["hermes_config"]
    if config is None:
        return [finding(
            "hermes_runtime_unverified",
            agent_name,
            "hermes",
            skill_alias,
            "Hermes override requires hermes_config for profile-scoped runtime validation",
        )], None

    frontmatter, detail = _parse_frontmatter(
        (override["source"] / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    )
    if detail is not None:
        return [finding("hermes_runtime_unverified", agent_name, "hermes", skill_alias, detail)], None
    expected_name = frontmatter["name"]
    expected_description = frontmatter["description"]
    runtime_runner = runner or _run_hermes_runtime
    try:
        result = runtime_runner(config.parent, expected_name)
    except Exception as exc:
        return [finding(
            "hermes_runtime_unverified",
            agent_name,
            "hermes",
            skill_alias,
            f"Hermes import or command failed: {exc}",
        )], None

    if not isinstance(result, dict) or not isinstance(result.get("prompt"), str) or not isinstance(result.get("view"), dict):
        return [finding(
            "hermes_runtime_unverified",
            agent_name,
            "hermes",
            skill_alias,
            "Hermes runner result requires string prompt and object view",
        )], None

    prompt = result["prompt"]
    view = result["view"]
    content = view.get("content")
    prompt_description = expected_description if len(expected_description) <= 60 else expected_description[:57] + "..."
    prompt_matches = expected_name in prompt and prompt_description in prompt
    view_matches = view.get("success") is True and isinstance(content, str) and expected_description in content
    if not prompt_matches or not view_matches:
        reason = view.get("error") if view.get("success") is not True else "resolved skill content did not match assigned source"
        return [finding(
            "hermes_runtime_shadowed",
            agent_name,
            "hermes",
            skill_alias,
            f"prompt_match={prompt_matches}, skill_view_match={view_matches}: {reason}",
        )], None

    return [], {
        "agent": agent_name,
        "skill": skill_alias,
        "name": expected_name,
        "description": expected_description,
    }


def validate_hermes_surface(agent: dict[str, Any]) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    config = agent["hermes_config"]
    if config is None:
        return [finding("hermes_runtime_unverified", agent["name"], "hermes", "", "missing Hermes profile configuration")], None
    expected: dict[str, str] = {}
    for alias, source in agent["skills"].items():
        frontmatter, detail = _parse_frontmatter((source / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
        if detail is not None:
            return [finding("hermes_runtime_unverified", agent["name"], "hermes", alias, detail)], None
        expected[frontmatter["name"]] = frontmatter["description"]
    try:
        result = _run_hermes_surface_runtime(config.parent, sorted(expected))
    except Exception as exc:
        return [finding("hermes_runtime_unverified", agent["name"], "hermes", "", f"Hermes profile discovery failed: {exc}")], None
    prompt = result.get("prompt") if isinstance(result, dict) else None
    views = result.get("views") if isinstance(result, dict) else None
    if not isinstance(prompt, str) or not isinstance(views, dict):
        return [finding("hermes_runtime_unverified", agent["name"], "hermes", "", "Hermes surface runner requires prompt and views objects")], None
    missing: list[str] = []
    for name, description in expected.items():
        compact = description if len(description) <= 60 else description[:57] + "..."
        view = views.get(name)
        if name not in prompt or compact not in prompt or not isinstance(view, dict) or view.get("success") is not True or view.get("name") != name or view.get("description") != description:
            missing.append(name)
    if missing:
        return [finding("hermes_runtime_shadowed", agent["name"], "hermes", ",".join(missing[:5]), f"{len(missing)} baseline skills failed live Hermes discovery")], None
    return [], {"agent": agent["name"], "skills": len(expected)}


def validate_codex_surface(agent: dict[str, Any]) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    codex = shutil.which("codex")
    if codex is None:
        return [finding("codex_runtime_unverified", agent["name"], "codex", "", "codex CLI is not on PATH")], None
    completed = subprocess.run(
        [codex, "debug", "prompt-input", CODEX_PROBE],
        cwd=agent["home"],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        return [finding("codex_runtime_unverified", agent["name"], "codex", "", detail)], None
    try:
        prompt = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return [finding("codex_runtime_unverified", agent["name"], "codex", "", f"invalid prompt input JSON: {exc}")], None
    rendered = json.dumps(prompt, ensure_ascii=False)
    roots = {
        alias: Path(root).resolve()
        for alias, root in re.findall(r"- `([^`]+)` = `([^`]+)`", rendered)
    }
    missing: list[str] = []
    for name, source in agent["skills"].items():
        candidates = [str(source / "SKILL.md")]
        for alias, root in roots.items():
            # Codex renders the projected alias path.  The canonical source is
            # commonly reached through a symlink and was resolved while the
            # manifest loaded, so source.relative_to(root) alone falsely
            # reported every projected skill as absent.
            candidates.append(f"{alias}/{name}/SKILL.md")
            try:
                candidates.append(f"{alias}/{source.relative_to(root) / 'SKILL.md'}")
            except ValueError:
                continue
        if not any(candidate in rendered for candidate in candidates):
            missing.append(name)
    if missing:
        return [finding("codex_runtime_missing_skill", agent["name"], "codex", ",".join(missing[:5]), f"{len(missing)} baseline skills absent from the live Codex prompt")], None
    return [], {"agent": agent["name"], "skills": len(agent["skills"])}


def run(manifest_path: str | Path, apply: bool = False, hermes_runner: Any = None, smoke: bool = False, codex_smoke: bool = False) -> dict[str, Any]:
    path = Path(manifest_path).expanduser().resolve()
    agents, issues, baseline = _load_manifest(path)
    issues.extend(_validate_sources(agents))
    resolved_skill_hashes = {agent["name"]: _skills_hash(agent["skills"]) for agent in agents}
    baseline_hash = _skills_hash(baseline) if baseline is not None else None
    if baseline_hash is not None:
        for agent in agents:
            if resolved_skill_hashes[agent["name"]] != baseline_hash:
                issues.append(finding("baseline_resolution_drift", agent["name"], "manifest", "", "resolved skills differ from universe_baseline"))
    if issues:
        return {
            "mode": "apply" if apply else "check",
            "agents": len(agents),
            "changes": 0,
            "findings": issues,
            "hermes_runtime_verified": [],
            "hermes_surface_runtime_verified": [],
            "codex_surface_runtime_verified": [],
            "baseline_skill_count": len(baseline) if baseline is not None else None,
            "baseline_hash": baseline_hash,
            "resolved_skill_hashes": resolved_skill_hashes,
        }

    changes = 0
    hermes_runtime_verified: list[dict[str, str]] = []
    hermes_surface_runtime_verified: list[dict[str, Any]] = []
    codex_surface_runtime_verified: list[dict[str, Any]] = []
    for agent in agents:
        for adapter in ADAPTERS:
            adapter_issues, adapter_changes = _project_adapter(agent, adapter, apply)
            issues.extend(adapter_issues)
            changes += adapter_changes
        if agent["hermes_config"] is not None:
            hermes_issues, hermes_changes = ensure_hermes_external_dir(
                agent["hermes_config"], agent["home"] / ADAPTERS["open_agent"], apply, agent["name"]
            )
            issues.extend(hermes_issues)
            changes += hermes_changes
        for override in agent["hermes_overrides"]:
            override_issues, override_changes = apply_hermes_override(override, apply, agent["name"])
            issues.extend(override_issues)
            changes += override_changes
            runtime_issues, verified = validate_hermes_runtime(agent, override, hermes_runner)
            issues.extend(runtime_issues)
            if verified is not None:
                hermes_runtime_verified.append(verified)
        if smoke:
            surface_issues, surface_verified = validate_hermes_surface(agent)
            issues.extend(surface_issues)
            if surface_verified is not None:
                hermes_surface_runtime_verified.append(surface_verified)
        if codex_smoke:
            surface_issues, surface_verified = validate_codex_surface(agent)
            issues.extend(surface_issues)
            if surface_verified is not None:
                codex_surface_runtime_verified.append(surface_verified)
    return {
        "mode": "apply" if apply else "check",
        "agents": len(agents),
        "changes": changes,
        "findings": issues,
        "hermes_runtime_verified": hermes_runtime_verified,
        "hermes_surface_runtime_verified": hermes_surface_runtime_verified,
        "codex_surface_runtime_verified": codex_surface_runtime_verified,
        "baseline_skill_count": len(baseline) if baseline is not None else None,
        "baseline_hash": baseline_hash,
        "resolved_skill_hashes": resolved_skill_hashes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest", type=Path, nargs="?", help="manifest path (legacy positional form)")
    parser.add_argument("--manifest", dest="manifest_flag", type=Path, help="canonical assignment manifest path")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="validate without changing files (default)")
    mode.add_argument("--apply", action="store_true", help="create or repair managed projections")
    parser.add_argument("--smoke", action="store_true", help="exercise live Hermes discovery for every profile and baseline skill")
    parser.add_argument("--codex-smoke", action="store_true", help="exercise live Codex prompt discovery for every agent root and baseline skill")
    args = parser.parse_args()
    manifest = args.manifest_flag or args.manifest
    if manifest is None:
        parser.error("a manifest path is required")
    result = run(manifest, apply=args.apply, smoke=args.smoke, codex_smoke=args.codex_smoke)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
