"""Canonical Beacon Skill reader, renderer, and content-addressed installer."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


CONVERTER_VERSION = 1
TARGETS = ("claude", "codex")
CLAUDE_FIELDS = {"args"}
OPENAI_FIELDS = {"tags", "sandbox_hint", "policy"}


class SkillConversionError(ValueError):
    pass


@dataclass(frozen=True)
class CanonicalSkill:
    name: str
    description: str
    body: str
    root: Path
    claude: dict[str, str]


def _parse_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_flat_yaml(text: str, *, allowed: set[str], label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw[:1].isspace() or ":" not in raw:
            raise SkillConversionError(f"{label}:{lineno}: expected a top-level key")
        key, value = raw.split(":", 1)
        key = key.strip()
        if key not in allowed:
            raise SkillConversionError(f"{label}:{lineno}: unsupported field {key!r}")
        if key in result:
            raise SkillConversionError(f"{label}:{lineno}: duplicate field {key!r}")
        result[key] = _parse_scalar(value)
    return result


def _validate_openai_yaml(path: Path) -> None:
    if not path.exists():
        return
    seen: set[str] = set()
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or raw[:1].isspace():
            continue
        if ":" not in raw:
            raise SkillConversionError(f"{path}:{lineno}: expected a top-level key")
        key = raw.split(":", 1)[0].strip()
        if key not in OPENAI_FIELDS:
            raise SkillConversionError(f"{path}:{lineno}: unsupported field {key!r}")
        if key in seen:
            raise SkillConversionError(f"{path}:{lineno}: duplicate field {key!r}")
        seen.add(key)


def _split_skill_markdown(text: str, path: Path) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise SkillConversionError(f"{path}: missing YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise SkillConversionError(f"{path}: unterminated YAML frontmatter")
    metadata = _parse_flat_yaml(
        text[4:end], allowed={"name", "description"}, label=str(path)
    )
    if set(metadata) != {"name", "description"}:
        missing = sorted({"name", "description"} - set(metadata))
        raise SkillConversionError(f"{path}: missing required field(s): {', '.join(missing)}")
    return metadata, text[end + 5 :]


def read_canonical_skill(root: Path) -> CanonicalSkill:
    root = Path(root)
    skill_md = root / "SKILL.md"
    if not skill_md.is_file():
        raise SkillConversionError(f"missing canonical Skill file: {skill_md}")
    if root.is_symlink():
        raise SkillConversionError(f"canonical Skill directory must not be a symlink: {root}")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SkillConversionError(f"canonical Skill resources must not be symlinks: {path}")

    metadata, body = _split_skill_markdown(skill_md.read_text(encoding="utf-8"), skill_md)
    name = metadata["name"]
    if name != root.name:
        raise SkillConversionError(
            f"Skill name {name!r} must match directory name {root.name!r}"
        )

    claude_path = root / "clients" / "claude.yaml"
    claude = (
        _parse_flat_yaml(
            claude_path.read_text(encoding="utf-8"),
            allowed=CLAUDE_FIELDS,
            label=str(claude_path),
        )
        if claude_path.exists()
        else {}
    )
    _validate_openai_yaml(root / "agents" / "openai.yaml")
    return CanonicalSkill(name, metadata["description"], body, root, claude)


def render_claude(skill: CanonicalSkill) -> bytes:
    lines = ["---", f"name: {skill.name}", f"description: {skill.description}"]
    for key in sorted(skill.claude):
        lines.append(f"{key}: {json.dumps(skill.claude[key], ensure_ascii=False)}")
    lines.append("---")
    body = skill.body if skill.body.endswith("\n") else skill.body + "\n"
    return ("\n".join(lines) + "\n" + body).encode("utf-8")


def render_codex(skill: CanonicalSkill, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(skill.root / "SKILL.md", destination / "SKILL.md")
    for dirname in ("scripts", "references", "assets", "agents"):
        source = skill.root / dirname
        if source.is_dir():
            shutil.copytree(source, destination / dirname)


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    root = Path(root)
    if root.is_file():
        return _hash_bytes(root.read_bytes())
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{path.stat().st_mode & 0o777:o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _state_path(home: Path) -> Path:
    override = os.environ.get("BEACON_SKILL_INSTALL_STATE", "").strip()
    return Path(override).expanduser() if override else home / ".beacon" / "skill-installs.json"


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": 1, "installs": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillConversionError(f"invalid install state {path}: {exc}") from exc
    if state.get("schema_version") != 1 or not isinstance(state.get("installs"), dict):
        raise SkillConversionError(f"unsupported install state schema: {path}")
    return state


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _destination(home: Path, target: str, name: str) -> Path:
    if target == "claude":
        return home / ".claude" / "skills" / f"{name}.md"
    return home / ".agents" / "skills" / name


def _entry_key(target: str, name: str) -> str:
    return f"{target}:{name}"


def install_skill(
    source: Path,
    *,
    targets: tuple[str, ...],
    home: Path,
    dry_run: bool = False,
    force: bool = False,
    adopt: bool = False,
) -> list[dict]:
    if force and adopt:
        raise SkillConversionError("--force and --adopt are mutually exclusive")
    if not targets or any(target not in TARGETS for target in targets):
        raise SkillConversionError("target must be claude, codex, or both")

    skill = read_canonical_skill(source)
    home = Path(home)
    state_path = _state_path(home)
    state = _load_state(state_path)
    source_hash = hash_tree(skill.root)
    staging_parent = None
    if not dry_run:
        staging_parent = home / ".beacon"
        staging_parent.mkdir(parents=True, exist_ok=True)
    staged_root = Path(tempfile.mkdtemp(
        prefix="beacon-skill-render-",
        dir=str(staging_parent) if staging_parent else None,
    ))
    plans: list[dict] = []
    try:
        for target in targets:
            staged = staged_root / target
            if target == "claude":
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_bytes(render_claude(skill))
            else:
                render_codex(skill, staged)
            destination = _destination(home, target, skill.name)
            rendered_hash = hash_tree(staged)
            key = _entry_key(target, skill.name)
            prior = state["installs"].get(key, {})
            current_hash = hash_tree(destination) if destination.exists() else ""
            conflict = bool(
                destination.exists()
                and (not prior or current_hash != prior.get("rendered_hash", ""))
            )
            if conflict and not (force or adopt):
                raise SkillConversionError(
                    f"destination was modified outside Beacon: {destination}; "
                    "use --force to replace or --adopt to keep it"
                )
            action = "adopted" if conflict and adopt else (
                "unchanged" if current_hash == rendered_hash else "installed"
            )
            plans.append({
                "target": target,
                "destination": destination,
                "staged": staged,
                "rendered_hash": current_hash if action == "adopted" else rendered_hash,
                "action": action,
                "key": key,
                "warnings": (
                    ["sub-resources are effective only for the Codex target"]
                    if target == "claude"
                    and any((skill.root / name).is_dir() for name in ("scripts", "references", "assets"))
                    else []
                ),
            })

        if dry_run:
            return [{k: str(v) if isinstance(v, Path) else v for k, v in plan.items()
                     if k not in {"staged", "key"}} for plan in plans]

        backups: list[tuple[Path, Path | None]] = []
        try:
            for index, plan in enumerate(plans):
                destination = plan["destination"]
                if plan["action"] in {"adopted", "unchanged"}:
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                backup = None
                if destination.exists():
                    backup = staged_root / f"backup-{index}"
                    os.replace(destination, backup)
                backups.append((destination, backup))
                os.replace(plan["staged"], destination)
            for plan in plans:
                state["installs"][plan["key"]] = {
                    "converter_version": CONVERTER_VERSION,
                    "destination": str(plan["destination"]),
                    "rendered_hash": plan["rendered_hash"],
                    "source_hash": source_hash,
                    "target": plan["target"],
                }
            _write_json_atomic(state_path, state)
        except Exception:
            for destination, backup in reversed(backups):
                if destination.exists():
                    if destination.is_dir():
                        shutil.rmtree(destination)
                    else:
                        destination.unlink()
                if backup is not None and backup.exists():
                    os.replace(backup, destination)
            raise
        return [{k: str(v) if isinstance(v, Path) else v for k, v in plan.items()
                 if k not in {"staged", "key"}} for plan in plans]
    finally:
        shutil.rmtree(staged_root, ignore_errors=True)


def prune_skill(
    name: str,
    *,
    targets: tuple[str, ...],
    home: Path,
    dry_run: bool = False,
    force: bool = False,
) -> list[dict]:
    """Remove explicitly named, managed install snapshots."""
    if not targets or any(target not in TARGETS for target in targets):
        raise SkillConversionError("target must be claude, codex, or both")
    home = Path(home)
    state_path = _state_path(home)
    state = _load_state(state_path)
    plans: list[dict] = []
    for target in targets:
        key = _entry_key(target, name)
        prior = state["installs"].get(key)
        destination = _destination(home, target, name)
        if not prior:
            plans.append({
                "target": target, "destination": str(destination), "action": "unmanaged"
            })
            continue
        if destination.exists():
            current_hash = hash_tree(destination)
            if current_hash != prior.get("rendered_hash", "") and not force:
                raise SkillConversionError(
                    f"destination was modified outside Beacon: {destination}; "
                    "use --force to prune it"
                )
        plans.append({
            "target": target, "destination": str(destination), "action": "pruned",
            "key": key,
        })
    if dry_run:
        return [{k: v for k, v in plan.items() if k != "key"} for plan in plans]

    transaction_parent = home / ".beacon"
    transaction_parent.mkdir(parents=True, exist_ok=True)
    transaction_root = Path(tempfile.mkdtemp(
        prefix="beacon-skill-prune-", dir=str(transaction_parent)
    ))
    backups: list[tuple[Path, Path]] = []
    original_state = json.loads(json.dumps(state))
    try:
        try:
            for index, plan in enumerate(plans):
                if plan["action"] != "pruned":
                    continue
                destination = Path(plan["destination"])
                if destination.exists():
                    backup = transaction_root / f"backup-{index}"
                    os.replace(destination, backup)
                    backups.append((destination, backup))
                state["installs"].pop(plan["key"], None)
            _write_json_atomic(state_path, state)
        except Exception:
            for destination, backup in reversed(backups):
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, destination)
            if state_path.exists() or original_state["installs"]:
                _write_json_atomic(state_path, original_state)
            raise
        return [{k: v for k, v in plan.items() if k != "key"} for plan in plans]
    finally:
        shutil.rmtree(transaction_root, ignore_errors=True)


def resolve_canonical_root() -> Path:
    override = os.environ.get("BEACON_SKILLS_SOURCE", "").strip()
    if override:
        return Path(override).expanduser()
    repo_root = Path(__file__).resolve().parent.parent
    source = repo_root / "shared" / "skills"
    if source.is_dir():
        return source
    bundled = Path(__file__).resolve().parent / "_bundled_shared_skills"
    if bundled.is_dir():
        return bundled
    raise SkillConversionError(
        "canonical shared/skills directory not found; set BEACON_SKILLS_SOURCE"
    )
