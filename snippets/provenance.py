"""Capture portable provenance metadata for data-processing outputs.

The module records Git repository state, classifies input paths by storage
backend, and formats compact history entries for CF-style metadata and xarray
objects. Helpers with a ``public_`` prefix remove local-only details before the
metadata is written into public outputs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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
    """Snapshot of a Git repository at one point in time.

    Attributes:
        repo_root: Absolute repository root on the local filesystem.
        commit: Current HEAD commit, or ``None`` if it cannot be read.
        branch: Current branch name, or ``None`` for detached HEAD.
        remote_url: Canonical remote URL for the configured remote.
        dirty: Whether the repository has staged, unstaged, or untracked changes.
        dirty_marker: ``"+dirty"`` when dirty, otherwise an empty string.
        status_short: Raw ``git status --porcelain`` output.
        diff_hash: Hash of dirty status and diff text when requested.
    """

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
    """Snapshot of an input path and the metadata that identifies it.

    Attributes:
        path: Absolute local path that was inspected.
        exists: Whether the path exists.
        kind: Path kind: ``"file"``, ``"directory"``, ``"missing"``, or
            ``"other"``.
        backend: Storage backend inferred for the path.
        metadata: Backend-specific metadata such as DVC outputs, Git LFS OIDs,
            or directory manifest information.
        git_state: Repository state when the path is inside a Git repository.
        git_path: Repository-relative path when available.
        git_status: ``git status --porcelain`` output for this path.
        error: Non-fatal discovery error, if one occurred.
    """

    path: Path
    exists: bool
    kind: str
    backend: Backend
    metadata: dict[str, Any]
    git_state: GitState | None = None
    git_path: str | None = None
    git_status: str = ""
    error: str | None = None


def canonicalize_remote_url(remote_url: str | None) -> str | None:
    """Return a portable, reader-friendly form of a Git remote URL.

    HTTPS, ``git@host:path``, ``ssh://`` and ``github:owner/repo`` forms are
    normalized where possible. Empty input returns ``None``.
    """

    if not remote_url:
        return None
    remote_url = remote_url.strip()
    if not remote_url:
        return None

    def clean_path(path: str) -> str:
        path = path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return path

    if remote_url.startswith(("http://", "https://")):
        scheme, rest = remote_url.split("://", 1)
        return f"{scheme}://{clean_path(rest)}"

    match = re.match(r"git@([^:]+):(.+)", remote_url)
    if match:
        host, path = match.groups()
        return f"https://{host}/{clean_path(path)}"

    match = re.match(r"ssh://(?:[^@/]+@)?([^/]+)/(.+)", remote_url)
    if match:
        host, path = match.groups()
        if host == "github":
            host = "github.com"
        return f"https://{host}/{clean_path(path)}"

    if remote_url.startswith("github:"):
        return f"https://github.com/{clean_path(remote_url.removeprefix('github:'))}"

    return remote_url


def run_git(args: Sequence[str], cwd: Path | str) -> tuple[bool, str, str]:
    """Run a Git command and return ``(ok, stdout, error)``.

    Args:
        args: Git arguments without the leading ``git`` executable.
        cwd: Working directory used to run the command.

    Returns:
        A tuple containing success status, standard output, and an error
        message. The function never raises for a non-zero Git exit code.
    """

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
    """Find the Git repository root for a directory or one of its parents.

    Args:
        repo_dir: Starting directory. Defaults to the current working directory.
        max_parent_levels: Maximum number of parent directories to try after the
            starting directory.

    Returns:
        The resolved repository root, or ``None`` if no repository is found.
    """

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
    """Capture the current Git state for a repository.

    Args:
        repo_dir: Directory that must resolve directly to a Git repository.
        remote: Remote name used for the public remote URL.
        include_diff_hash: Whether to hash dirty status and diff text when the
            repository is dirty.

    Raises:
        RuntimeError: If ``repo_dir`` cannot be resolved to a Git repository.

    Returns:
        A :class:`GitState` snapshot.
    """

    repo_root = discover_repo_root(repo_dir, max_parent_levels=0)
    if repo_root is None:
        raise RuntimeError(f"Could not resolve git repository from {repo_dir}.")

    _, commit_output, _ = run_git(["rev-parse", "HEAD"], cwd=repo_root)
    _, branch_output, _ = run_git(["branch", "--show-current"], cwd=repo_root)
    remote_ok, remote_output, _ = run_git(["remote", "get-url", remote], cwd=repo_root)
    _, status_output, _ = run_git(["status", "--porcelain"], cwd=repo_root)

    branch = branch_output.strip() or None
    dirty = bool(status_output.strip())
    diff_hash = None
    if include_diff_hash and dirty:
        _, staged, _ = run_git(
            ["diff", "--cached", "--no-ext-diff", "--"], cwd=repo_root
        )
        _, unstaged, _ = run_git(["diff", "--no-ext-diff", "--"], cwd=repo_root)
        diff_payload = f"{status_output}\n{staged}\n{unstaged}"
        diff_hash = hashlib.sha256(
            diff_payload.encode("utf-8", errors="replace")
        ).hexdigest()

    return GitState(
        repo_root=repo_root,
        commit=commit_output.strip() or None,
        branch=branch,
        remote_url=canonicalize_remote_url(
            remote_output.strip() if remote_ok and remote_output.strip() else None
        ),
        dirty=dirty,
        dirty_marker="+dirty" if dirty else "",
        status_short=status_output.rstrip("\n"),
        diff_hash=diff_hash,
    )


def _repo_name_from_remote(remote_url: str | None) -> str | None:
    if not remote_url:
        return None
    remote_url = canonicalize_remote_url(remote_url) or remote_url
    path = remote_url.rstrip("/").removesuffix(".git").split("/")[-1]
    return path or None


def _repo_name_from_path(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return Path(path).name or None
    except TypeError:
        return None


def public_git_state(state: GitState | Mapping[str, Any]) -> dict[str, Any]:
    """Return a portable Git state record suitable for file metadata.

    Local-only fields such as ``repo_root`` are omitted. Dirty-only details such
    as ``diff_hash`` and ``status_short`` are included only when the state is
    dirty.
    """

    data = to_jsonable(state)
    remote_url = canonicalize_remote_url(data.get("remote_url") or data.get("remote"))
    dirty = bool(data.get("dirty") or data.get("git_dirty"))
    name = (
        data.get("name")
        or data.get("package")
        or _repo_name_from_remote(remote_url)
        or _repo_name_from_path(data.get("repo_root"))
        or _repo_name_from_path(data.get("label"))
    )
    result: dict[str, Any] = {
        "name": name,
        "commit": data.get("commit") or data.get("git_head"),
        "branch": data.get("branch") or data.get("git_branch"),
        "remote_url": remote_url,
        "dirty": dirty,
    }
    if dirty:
        result["dirty_marker"] = data.get("dirty_marker") or "+dirty"
        if data.get("diff_hash") or data.get("git_diff_hash"):
            result["diff_hash"] = data.get("diff_hash") or data.get("git_diff_hash")
        if data.get("status_short"):
            result["status_short"] = data.get("status_short")
    return {key: value for key, value in result.items() if value not in (None, "")}


def format_git_state(state: GitState | Mapping[str, Any]) -> str:
    """Format a compact one-line repository summary.

    The result is intended for human-readable history entries, for example
    ``snipGit@abc1234+dirty (main)``.
    """

    data = public_git_state(state)
    commit = data.get("commit") or "unknown"
    short = str(commit)[:12]
    dirty = bool(data.get("dirty"))
    marker = "+dirty" if dirty else ""
    branch = data.get("branch") or "detached"
    name = data.get("name")
    prefix = f"{name}@" if name else ""
    return f"{prefix}{short}{marker} ({branch})"


def public_input_path_state(
    state: InputPathState | Mapping[str, Any],
) -> dict[str, Any]:
    """Return a compact input path record without local-only repository roots.

    The public record keeps stable identifiers such as repository-relative
    paths, backend names, Git LFS OIDs, DVC output metadata, and directory
    manifests. Absolute repository roots are removed.
    """

    data = to_jsonable(state)
    metadata = data.get("metadata", {})
    public_metadata: dict[str, Any] = {}
    if metadata.get("directory"):
        public_metadata["directory"] = metadata["directory"]
    lfs = metadata.get("lfs") or {}
    if lfs.get("tracked_by_lfs") or lfs.get("is_pointer_file"):
        public_metadata["lfs"] = {
            key: value
            for key, value in lfs.items()
            if key in {"tracked_by_lfs", "is_pointer_file", "oid", "size"}
        }
    dvc = metadata.get("dvc") or {}
    if dvc.get("dvc_files") or dvc.get("outputs"):
        public_metadata["dvc"] = dvc

    result: dict[str, Any] = {
        "path": data.get("git_path") or data.get("path"),
        "exists": data.get("exists"),
        "kind": data.get("kind"),
        "backend": data.get("backend"),
    }
    if public_metadata:
        result["metadata"] = public_metadata
    if data.get("git_state"):
        result["git"] = public_git_state(data["git_state"])
    if data.get("git_status"):
        result["git_status"] = data["git_status"]
    if data.get("error"):
        result["error"] = data["error"]
    return {key: value for key, value in result.items() if value not in (None, "")}


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


def _lfs_metadata(
    path: Path, repo_root: Path | None, rel: str | None
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"is_pointer_file": False, "tracked_by_lfs": False}
    if path.is_file():
        try:
            pointer = _parse_lfs_pointer(
                path.read_text(encoding="utf-8", errors="replace")[:512]
            )
        except OSError as err:
            pointer = None
            metadata["pointer_error"] = str(err)
        if pointer is not None:
            metadata.update(pointer)

    if repo_root is None or rel is None:
        return metadata

    attr_ok, attr_output, _ = run_git(
        ["check-attr", "filter", "--", rel], cwd=repo_root
    )
    if attr_ok and attr_output.strip().endswith("filter: lfs"):
        metadata["tracked_by_lfs"] = True

    lfs_ok, lfs_output, lfs_error = run_git(
        ["lfs", "ls-files", "--long", "--", rel], cwd=repo_root
    )
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


def _dvc_metadata(
    path: Path, repo_root: Path | None, rel: str | None
) -> dict[str, Any]:
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
            metadata.setdefault("errors", []).append(
                {"path": str(candidate), "error": str(err)}
            )
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


def summarize_directory(
    path: Path | str, *, max_entries: int = 20_000
) -> dict[str, Any]:
    """Summarize a directory without embedding a full file listing.

    Args:
        path: Directory to inspect.
        max_entries: Maximum number of files included in the manifest hash.

    Returns:
        File count, total bytes, manifest hash, hash kind, truncation flag, and
        the entry limit used for hashing.
    """

    root = Path(path)
    file_count = 0
    total_bytes = 0
    digest = hashlib.sha256()
    truncated = False
    for child in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda p: p.as_posix(),
    ):
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
    """Inspect one input path and classify its provenance backend.

    Backend priority is DVC, Git LFS, Git, filesystem, then unknown. Directory
    inputs also receive a manifest summary.

    Args:
        path: File or directory to inspect.

    Returns:
        An :class:`InputPathState` snapshot.
    """

    target = Path(path).expanduser().resolve()
    kind = _path_kind(target)
    repo_root = discover_repo_root(
        target if target.exists() else target.parent, max_parent_levels=8
    )
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
    """Inspect multiple input paths.

    Args:
        paths: File or directory paths to inspect.

    Returns:
        One :class:`InputPathState` per input path, preserving input order.
    """

    return [get_input_path_state(path) for path in paths]


def to_jsonable(value: Any) -> Any:
    """Convert common Python objects to JSON-serializable values.

    Dataclasses, paths, mappings, and iterables are converted recursively.
    Unknown objects fall back to ``str(value)``.
    """

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


def public_provenance(value: Any) -> Any:
    """Return provenance metadata intended to be burned into public outputs.

    The conversion recursively removes local-only fields and normalizes known
    provenance structures. It accepts dataclasses, mappings, sequences, and
    scalar values.
    """

    if isinstance(value, InputPathState):
        return public_input_path_state(value)
    if isinstance(value, GitState):
        return public_git_state(value)
    data = to_jsonable(value)
    if isinstance(data, Mapping):
        result: dict[str, Any] = {}
        for key, item in data.items():
            if key in {"history_entry", "repo_root", "source_path"}:
                continue
            if key in {"git_head", "git_branch", "git_dirty", "git_diff_hash"}:
                continue
            if key == "software_repos" and isinstance(item, Iterable):
                result[key] = [public_git_state(state) for state in item]
                continue
            if key == "input_paths" and isinstance(item, Iterable):
                result[key] = [public_input_path_state(state) for state in item]
                continue
            if key == "remote_url":
                result[key] = canonicalize_remote_url(str(item)) if item else None
                continue
            if key == "diff_hash" and not data.get("dirty"):
                continue
            if key == "status_short" and not item:
                continue
            result[str(key)] = public_provenance(item)
        return {key: item for key, item in result.items() if item not in (None, "")}
    if isinstance(data, list):
        return [public_provenance(item) for item in data]
    return data


def _clean_command_parts(parts: Sequence[str]) -> list[str]:
    cleaned = []
    skip_next = False
    for part in [str(item) for item in parts]:
        if skip_next:
            skip_next = False
            continue
        if part == "--provenance-json":
            skip_next = True
            continue
        if part.startswith("--provenance-json="):
            continue
        cleaned.append(part)

    if not cleaned:
        return cleaned

    first = Path(cleaned[0])
    if first.name == "downscaling.py" and "c4v_utils" in first.parts:
        return ["python", "-m", "c4v_utils.downscaling", *cleaned[1:]]
    return cleaned


def _command_text(command: str | Sequence[str] | None) -> str:
    if command is None:
        return "unknown command"
    if isinstance(command, str):
        return command
    return shlex.join(_clean_command_parts(command))


def build_cf_history_entry(
    command: str | Sequence[str] | None = None,
    *,
    git_state: GitState | Mapping[str, Any] | None = None,
    git_states: Sequence[GitState | Mapping[str, Any]] = (),
    input_states: Sequence[InputPathState | Mapping[str, Any]] = (),
    timestamp: datetime | None = None,
    include_inputs: bool = False,
) -> str:
    """Build a CF-style history entry.

    Args:
        command: Command text or argument sequence that produced the output.
        git_state: Primary software repository state.
        git_states: Additional software repository states.
        input_states: Input path states to summarize when ``include_inputs`` is
            true.
        timestamp: Timestamp for the history entry. Naive datetimes are treated
            as UTC.
        include_inputs: Whether to include compact input backend summaries.

    Returns:
        A semicolon-separated history line suitable for a CF ``history`` field.
    """

    when = timestamp or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    parts = [when.astimezone(timezone.utc).isoformat(timespec="seconds")]
    parts.append(_command_text(command))
    states = list(git_states)
    if git_state is not None:
        states.insert(0, git_state)
    if states:
        parts.append(
            "software=" + ", ".join(format_git_state(state) for state in states)
        )
    if include_inputs and input_states:
        compact = []
        for state in input_states:
            data = to_jsonable(state)
            path_name = Path(data.get("path", "unknown")).name
            backend = data.get("backend", "unknown")
            compact.append(
                f"{path_name}:{backend}"
            )
        parts.append("inputs=" + ", ".join(compact))
    return "; ".join(parts)


def append_cf_history(existing: str | None, entry: str) -> str:
    """Prepend a new entry to existing CF history text."""

    existing = (existing or "").strip()
    if not existing:
        return entry
    return f"{entry}\n{existing}"


def append_xarray_history(obj: Any, entry: str, *, copy: bool = False) -> Any:
    """Prepend a history entry to an xarray-like object's attrs.

    Args:
        obj: Object with an ``attrs`` mapping.
        entry: History entry to prepend.
        copy: Whether to call ``obj.copy()`` before mutating attributes.

    Returns:
        The original object, or the copied object when ``copy`` is true.
    """

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
    """Validate that repositories are clean unless dirty state is allowed.

    Args:
        repos: Repository paths to inspect.
        allow_dirty: Return dirty states instead of raising.
        missing_ok: Ignore missing paths and non-repositories when true.

    Raises:
        RuntimeError: If a required path is missing, a repository cannot be
        resolved, or a repository is dirty while ``allow_dirty`` is false.

    Returns:
        Git states for repositories that were found and inspected.
    """

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
    """Write provenance payload as stable, pretty-printed JSON."""

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        json.dump(to_jsonable(payload), handle, indent=2, sort_keys=True)
        handle.write("\n")


def read_provenance_json(path: Path | str) -> dict[str, Any]:
    """Read a provenance JSON file."""

    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def software_summary(states: Sequence[GitState | Mapping[str, Any]]) -> str:
    """Format multiple Git states as a comma-separated software summary."""

    return ", ".join(format_git_state(state) for state in states)


def env_allows_dirty(var: str = "PROV_TRACK_ALLOW_DIRTY") -> bool:
    """Return whether an environment variable opts into dirty repositories."""

    return os.environ.get(var, "").strip().lower() in {"1", "true", "yes", "on"}
