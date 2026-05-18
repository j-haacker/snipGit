from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


Backend = Literal["git-lfs", "dvc", "git", "filesystem", "unknown"]


@dataclass(frozen=True)
class GitState:
    repo_root: Path
    commit: str | None
    branch: str | None
    remote_url: str | None
    dirty: bool
    dirty_marker: str
    status_short: str
    diff_hash: str | None = None


@dataclass(frozen=True)
class InputPathState:
    path: Path
    exists: bool
    kind: str
    backend: Backend
    metadata: dict[str, Any]
    git_state: GitState | None = None
    git_path: str | None = None
    git_status: str = ""
    error: str | None = None


def run_git(args: Sequence[str], cwd: Path | str) -> tuple[bool, str, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=Path(cwd),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as err:
        return False, "", str(err)
    if proc.returncode != 0:
        error = proc.stderr.strip() or proc.stdout.strip()
        if not error:
            error = f"git {' '.join(args)} failed with exit code {proc.returncode}"
        return False, proc.stdout, error
    return True, proc.stdout, ""


def discover_repo_root(
    repo_dir: Path | str | None = None,
    *,
    max_parent_levels: int = 3,
) -> Path | None:
    start = Path.cwd() if repo_dir is None else Path(repo_dir).expanduser()
    candidates = [start.resolve()]
    current = candidates[0]
    for _ in range(max(0, max_parent_levels)):
        parent = current.parent
        if parent == current:
            break
        candidates.append(parent)
        current = parent
    for candidate in candidates:
        ok, output, _ = run_git(["rev-parse", "--show-toplevel"], cwd=candidate)
        if ok and output.strip():
            return Path(output.strip()).resolve()
    return None


def get_git_state(
    repo_dir: Path | str = ".",
    *,
    remote: str = "origin",
    include_diff_hash: bool = True,
) -> GitState:
    repo_root = discover_repo_root(repo_dir, max_parent_levels=0)
    if repo_root is None:
        raise RuntimeError(f"Could not resolve git repository from {repo_dir}.")

    _, commit_output, _ = run_git(["rev-parse", "HEAD"], cwd=repo_root)
    _, branch_output, _ = run_git(["branch", "--show-current"], cwd=repo_root)
    remote_ok, remote_output, _ = run_git(["remote", "get-url", remote], cwd=repo_root)
    _, status_output, _ = run_git(["status", "--porcelain"], cwd=repo_root)

    diff_hash = None
    if include_diff_hash:
        _, staged, _ = run_git(["diff", "--cached", "--no-ext-diff", "--"], cwd=repo_root)
        _, unstaged, _ = run_git(["diff", "--no-ext-diff", "--"], cwd=repo_root)
        diff_payload = f"{status_output}\n{staged}\n{unstaged}"
        diff_hash = hashlib.sha256(diff_payload.encode("utf-8", errors="replace")).hexdigest()

    branch = branch_output.strip() or None
    dirty = bool(status_output.strip())
    return GitState(
        repo_root=repo_root,
        commit=commit_output.strip() or None,
        branch=branch,
        remote_url=remote_output.strip() if remote_ok and remote_output.strip() else None,
        dirty=dirty,
        dirty_marker="+dirty" if dirty else "",
        status_short=status_output.rstrip("\n"),
        diff_hash=diff_hash,
    )


def format_git_state(state: GitState | Mapping[str, Any]) -> str:
    data = to_jsonable(state)
    commit = data.get("commit") or data.get("git_head") or "unknown"
    short = str(commit)[:12]
    dirty = bool(data.get("dirty") or data.get("git_dirty"))
    marker = "+dirty" if dirty else ""
    branch = data.get("branch") or data.get("git_branch") or "detached"
    remote = data.get("remote_url") or data.get("remote") or "no-remote"
    return f"{short}{marker} ({branch}; {remote})"


def _path_kind(path: Path) -> str:
    if not path.exists():
        return "missing"
    if path.is_dir():
        return "directory"
    if path.is_file():
        return "file"
    return "other"


def _repo_relative(path: Path, repo_root: Path) -> str | None:
    try:
        rel = path.resolve().relative_to(repo_root)
    except (OSError, ValueError):
        return None
    return rel.as_posix()


def _git_status_for_path(repo_root: Path, rel: str) -> str:
    ok, output, _ = run_git(["status", "--porcelain", "--", rel], cwd=repo_root)
    return output.rstrip("\n") if ok else ""


def _is_git_tracked(repo_root: Path, rel: str) -> bool:
    ok, _, _ = run_git(["ls-files", "--error-unmatch", "--", rel], cwd=repo_root)
    return ok


def _parse_lfs_pointer(text: str) -> dict[str, Any] | None:
    lines = [line.strip() for line in text.splitlines()]
    if not lines or lines[0] != "version https://git-lfs.github.com/spec/v1":
        return None
    pointer: dict[str, Any] = {"is_pointer_file": True}
    for line in lines[1:]:
        if line.startswith("oid sha256:"):
            pointer["oid"] = line.removeprefix("oid sha256:")
        elif line.startswith("size "):
            try:
                pointer["size"] = int(line.removeprefix("size "))
            except ValueError:
                pointer["size"] = line.removeprefix("size ")
    return pointer


def _lfs_metadata(path: Path, repo_root: Path | None, rel: str | None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"is_pointer_file": False, "tracked_by_lfs": False}
    if path.is_file():
        try:
            pointer = _parse_lfs_pointer(path.read_text(encoding="utf-8", errors="replace")[:512])
        except OSError as err:
            pointer = None
            metadata["pointer_error"] = str(err)
        if pointer is not None:
            metadata.update(pointer)

    if repo_root is None or rel is None:
        return metadata

    attr_ok, attr_output, _ = run_git(["check-attr", "filter", "--", rel], cwd=repo_root)
    if attr_ok and attr_output.strip().endswith("filter: lfs"):
        metadata["tracked_by_lfs"] = True

    lfs_ok, lfs_output, lfs_error = run_git(["lfs", "ls-files", "--long", "--", rel], cwd=repo_root)
    if lfs_ok and lfs_output.strip():
        parts = lfs_output.split()
        if parts:
            metadata["oid"] = parts[0]
            metadata["tracked_by_lfs"] = True
        metadata["lfs_ls_files"] = lfs_output.strip()
    elif lfs_error:
        metadata["lfs_error"] = lfs_error
    return metadata


def _simple_dvc_outputs(text: str) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("-"):
            if current:
                outputs.append(current)
            current = {}
            line = line[1:].strip()
        if current is None:
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"\'')
        if key in {"path", "md5", "hash", "etag", "size"}:
            current[key] = value
    if current:
        outputs.append(current)
    return outputs


def _dvc_metadata(path: Path, repo_root: Path | None, rel: str | None) -> dict[str, Any]:
    candidates: list[Path] = []
    if repo_root is not None and rel is not None:
        candidates.append(repo_root / f"{rel}.dvc")
        candidates.append(repo_root / "dvc.lock")
    candidates.append(path.with_name(f"{path.name}.dvc"))

    metadata: dict[str, Any] = {"dvc_files": [], "outputs": []}
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen or not candidate.exists() or not candidate.is_file():
            continue
        seen.add(candidate)
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            metadata.setdefault("errors", []).append({"path": str(candidate), "error": str(err)})
            continue
        if candidate.name == "dvc.lock" and rel is not None and rel not in text:
            continue
        dvc_info = {"path": str(candidate), "outputs": _simple_dvc_outputs(text)}
        if repo_root is not None:
            dvc_rel = _repo_relative(candidate, repo_root)
            if dvc_rel is not None:
                dvc_info["git_status"] = _git_status_for_path(repo_root, dvc_rel)
        metadata["dvc_files"].append(dvc_info)
        metadata["outputs"].extend(dvc_info["outputs"])
    return metadata


def summarize_directory(path: Path | str, *, max_entries: int = 20_000) -> dict[str, Any]:
    root = Path(path)
    file_count = 0
    total_bytes = 0
    digest = hashlib.sha256()
    truncated = False
    for child in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda p: p.as_posix()):
        file_count += 1
        try:
            stat = child.stat()
        except OSError:
            continue
        total_bytes += stat.st_size
        if file_count <= max_entries:
            rel = child.relative_to(root).as_posix()
            digest.update(f"{rel}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
        else:
            truncated = True
    return {
        "file_count": file_count,
        "total_bytes": total_bytes,
        "manifest_hash": digest.hexdigest(),
        "manifest_hash_kind": "paths-size-mtime-ns",
        "manifest_truncated": truncated,
        "max_entries": max_entries,
    }


def get_input_path_state(path: Path | str) -> InputPathState:
    target = Path(path).expanduser().resolve()
    kind = _path_kind(target)
    repo_root = discover_repo_root(target if target.exists() else target.parent, max_parent_levels=8)
    git_state = None
    rel = None
    git_status = ""
    metadata: dict[str, Any] = {}
    error = None

    if repo_root is not None:
        rel = _repo_relative(target, repo_root)
        if rel is not None:
            try:
                git_state = get_git_state(repo_root)
                git_status = _git_status_for_path(repo_root, rel)
            except RuntimeError as err:
                error = str(err)

    if kind == "directory":
        metadata["directory"] = summarize_directory(target)

    dvc = _dvc_metadata(target, repo_root, rel)
    lfs = _lfs_metadata(target, repo_root, rel)
    metadata.update({"lfs": lfs, "dvc": dvc})

    if dvc["dvc_files"]:
        backend: Backend = "dvc"
    elif lfs.get("tracked_by_lfs") or lfs.get("is_pointer_file"):
        backend = "git-lfs"
    elif repo_root is not None and rel is not None and _is_git_tracked(repo_root, rel):
        backend = "git"
    elif kind != "missing":
        backend = "filesystem"
    else:
        backend = "unknown"

    return InputPathState(
        path=target,
        exists=target.exists(),
        kind=kind,
        backend=backend,
        metadata=metadata,
        git_state=git_state,
        git_path=rel,
        git_status=git_status,
        error=error,
    )


def get_input_path_states(paths: Iterable[Path | str]) -> list[InputPathState]:
    return [get_input_path_state(path) for path in paths]


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, Iterable):
        return [to_jsonable(item) for item in value]
    return str(value)


def _command_text(command: str | Sequence[str] | None) -> str:
    if command is None:
        return "unknown command"
    if isinstance(command, str):
        return command
    return shlex.join([str(part) for part in command])


def build_cf_history_entry(
    command: str | Sequence[str] | None = None,
    *,
    git_state: GitState | Mapping[str, Any] | None = None,
    git_states: Sequence[GitState | Mapping[str, Any]] = (),
    input_states: Sequence[InputPathState | Mapping[str, Any]] = (),
    timestamp: datetime | None = None,
) -> str:
    when = timestamp or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    parts = [when.astimezone(timezone.utc).isoformat(timespec="seconds")]
    parts.append(_command_text(command))
    states = list(git_states)
    if git_state is not None:
        states.insert(0, git_state)
    if states:
        parts.append("software=" + ", ".join(format_git_state(state) for state in states))
    if input_states:
        compact = []
        for state in input_states:
            data = to_jsonable(state)
            compact.append(
                f"{Path(data.get('path', 'unknown')).name}:{data.get('backend', 'unknown')}"
            )
        parts.append("inputs=" + ", ".join(compact))
    return "; ".join(parts)


def append_cf_history(existing: str | None, entry: str) -> str:
    existing = (existing or "").strip()
    if not existing:
        return entry
    return f"{entry}\n{existing}"


def append_xarray_history(obj: Any, entry: str, *, copy: bool = False) -> Any:
    if copy:
        obj = obj.copy()
    obj.attrs["history"] = append_cf_history(obj.attrs.get("history"), entry)
    return obj


def enforce_clean_repos(
    repos: Iterable[Path | str],
    *,
    allow_dirty: bool = False,
    missing_ok: bool = True,
) -> list[GitState]:
    states: list[GitState] = []
    failures = []
    for repo in repos:
        repo_path = Path(repo).expanduser()
        if not repo_path.exists():
            if not missing_ok:
                failures.append(f"{repo}: path does not exist")
            continue
        try:
            state = get_git_state(repo_path)
        except RuntimeError as err:
            if not missing_ok:
                failures.append(str(err))
            continue
        states.append(state)
        if state.dirty and not allow_dirty:
            failures.append(f"{state.repo_root} is dirty:\n{state.status_short}")
    if failures:
        raise RuntimeError(
            "Dirty software repository state requires --allow-dirty.\n"
            + "\n\n".join(failures)
        )
    return states


def write_provenance_json(path: Path | str, payload: Mapping[str, Any]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        json.dump(to_jsonable(payload), handle, indent=2, sort_keys=True)
        handle.write("\n")


def read_provenance_json(path: Path | str) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def software_summary(states: Sequence[GitState | Mapping[str, Any]]) -> str:
    return ", ".join(format_git_state(state) for state in states)


def env_allows_dirty(var: str = "PROV_TRACK_ALLOW_DIRTY") -> bool:
    return os.environ.get(var, "").strip().lower() in {"1", "true", "yes", "on"}
