"""Reproduce a recorded provenance step in a fresh workspace."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Any


PRODUCT_ROOT_MARKERS = {"prepared", "factors", "adjusted", "weights"}


class ReproductionError(RuntimeError):
    """Raised when a reproduction setup cannot proceed."""


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def infer_run_root(provenance_path: str | Path) -> Path:
    path = Path(provenance_path).resolve()
    for parent in path.parents:
        if parent.name in PRODUCT_ROOT_MARKERS:
            return parent.parent
    return path.parent


def _resolve_run_ref(provenance_path: Path, ref: str | None) -> Path | None:
    if not ref:
        return None
    path = Path(ref)
    if path.is_absolute():
        return path
    return infer_run_root(provenance_path) / path


def _parse_sha256_sidecar(text: str) -> str | None:
    parts = text.strip().split()
    return parts[0] if parts else None


def _validation(
    report: dict[str, Any],
    *,
    name: str,
    path: Path,
    expected_sha256: str | None,
    required: bool = False,
) -> bool:
    item: dict[str, Any] = {
        "name": name,
        "path": str(path),
        "expected_sha256": expected_sha256,
    }
    if not path.exists():
        item["status"] = "missing"
        report["validations"].append(item)
        message = f"{name} is missing: {path}"
        (report["blockers"] if required else report["warnings"]).append(message)
        return False
    actual = sha256_file(path)
    item["actual_sha256"] = actual
    item["status"] = (
        "ok" if not expected_sha256 or actual == expected_sha256 else "mismatch"
    )
    report["validations"].append(item)
    if expected_sha256 and actual != expected_sha256:
        message = f"{name} checksum mismatch: {path}"
        (report["blockers"] if required else report["warnings"]).append(message)
        return False
    return True


def clean_command_parts(parts: list[str]) -> list[str]:
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
    return cleaned


def apply_input_maps(command: list[str], input_maps: dict[str, str]) -> list[str]:
    return [input_maps.get(part, part) for part in command]


def _resolve_existing_recorded_path(value: str, provenance_path: Path) -> Path | None:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve() if path.exists() else None
    bases = [Path.cwd().resolve(), *provenance_path.parents]
    for base in dict.fromkeys(bases):
        candidate = base / path
        if candidate.exists():
            return candidate.resolve()
    return None


def resolve_input_maps(
    *,
    record: dict[str, Any],
    provenance_path: Path,
    explicit_maps: dict[str, str],
    report: dict[str, Any],
) -> dict[str, str]:
    resolved_maps = dict(explicit_maps)
    report["resolved_inputs"] = []
    for item in record.get("input_paths") or []:
        original = str(item.get("path") or "")
        if not original or original in resolved_maps:
            continue
        resolved = _resolve_existing_recorded_path(original, provenance_path)
        if resolved is None:
            report["warnings"].append(
                f"Recorded input path could not be resolved: {original}"
            )
            continue
        resolved_maps[original] = str(resolved)
        report["resolved_inputs"].append(
            {
                "path": original,
                "resolved": str(resolved),
                "source": "recorded-input-path",
            }
        )
    return resolved_maps


def parse_key_value(items: list[str] | None) -> dict[str, str]:
    result = {}
    for item in items or []:
        if "=" not in item:
            raise ReproductionError(f"Expected KEY=VALUE, got {item!r}.")
        key, value = item.split("=", 1)
        if not key or not value:
            raise ReproductionError(f"Expected KEY=VALUE, got {item!r}.")
        result[key] = value
    return result


def _pixi_environment_block(lock_text: str, environment: str | None) -> str:
    if not environment:
        return lock_text
    lines = lock_text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line == f"  {environment}:":
            start = index
            break
    if start is None:
        return ""
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.startswith("  ") and not line.startswith("    ") and line.endswith(":"):
            end = index
            break
    return "\n".join(lines[start:end])


def pixi_local_path_dependencies(lock_text: str, environment: str | None) -> list[str]:
    block = _pixi_environment_block(lock_text, environment)
    paths = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- pypi: "):
            continue
        value = stripped.removeprefix("- pypi: ").strip()
        if value.startswith("../") or value.startswith("/"):
            paths.append(value)
    return sorted(set(paths))


def _repo_name(state: dict[str, Any]) -> str:
    name = state.get("name")
    if name:
        return str(name)
    remote = str(state.get("remote_url") or "").rstrip("/")
    if remote:
        return remote.removesuffix(".git").split("/")[-1]
    return "repo"


def _select_project_repo(
    repos: list[dict[str, Any]], project_repo: str | None
) -> dict[str, Any]:
    if not repos:
        raise ReproductionError("Provenance contains no software_repos entries.")
    if project_repo:
        for state in repos:
            if _repo_name(state) == project_repo:
                return state
        raise ReproductionError(
            f"Project repo {project_repo!r} not found in provenance."
        )
    return repos[0]


def _repo_source(state: dict[str, Any], sources: dict[str, str]) -> str | None:
    name = _repo_name(state)
    return (
        sources.get(name)
        or _matching_local_repo_source(state)
        or state.get("remote_url")
    )


def _git_value(repo: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _repo_contains_commit(repo: Path, commit: str | None) -> bool:
    if not commit:
        return False
    return _git_value(repo, ["cat-file", "-t", commit]) == "commit"


def _candidate_local_repos(name: str) -> list[Path]:
    cwd = Path.cwd().resolve()
    candidates = []
    if cwd.name == name:
        candidates.append(cwd)
    candidates.append(cwd.parent / name)
    return list(dict.fromkeys(candidates))


def _matching_local_repo_source(state: dict[str, Any]) -> str | None:
    name = _repo_name(state)
    commit = state.get("commit")
    for candidate in _candidate_local_repos(name):
        if not (candidate / ".git").exists():
            continue
        if not _repo_contains_commit(candidate, commit):
            continue
        return str(candidate)
    return None


def _local_repo_state(name: str) -> dict[str, Any] | None:
    for candidate in _candidate_local_repos(name):
        if not (candidate / ".git").exists():
            continue
        commit = _git_value(candidate, ["rev-parse", "HEAD"]) or None
        branch = _git_value(candidate, ["branch", "--show-current"]) or None
        status = _git_value(candidate, ["status", "--porcelain"])
        return {
            "name": name,
            "commit": commit,
            "branch": branch,
            "remote_url": str(candidate),
            "dirty": bool(status),
            "status_short": status,
            "not_in_provenance": True,
        }
    return None


def _branch_name(state: dict[str, Any]) -> str | None:
    branch = state.get("branch")
    if not branch or branch == "detached":
        return None
    return str(branch)


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    report: dict[str, Any],
    step: str,
) -> subprocess.CompletedProcess[str]:
    item = {"step": step, "command": command, "cwd": str(cwd) if cwd else None}
    report["commands"].append(item)
    proc = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    item["returncode"] = proc.returncode
    if proc.stdout:
        item["stdout"] = proc.stdout[-4000:]
    if proc.stderr:
        item["stderr"] = proc.stderr[-4000:]
    if proc.returncode != 0:
        raise ReproductionError(
            f"{step} failed with exit code {proc.returncode}: {shlex.join(command)}"
        )
    return proc


def _clone_or_resume_repo(
    *,
    state: dict[str, Any],
    destination: Path,
    sources: dict[str, str],
    report: dict[str, Any],
    resume: bool,
    reuse_existing: bool = False,
) -> bool:
    name = _repo_name(state)
    source = _repo_source(state, sources)
    if not source:
        raise ReproductionError(
            f"Repository {name!r} has no remote_url; "
            f"provide --repo-source {name}=PATH_OR_URL."
        )
    if destination.exists():
        commit = state.get("commit")
        head = _git_value(destination, ["rev-parse", "HEAD"]) or None
        if reuse_existing and (not commit or head == commit):
            report["adaptations"].append(
                {
                    "kind": "existing-editable-dependency",
                    "repo": name,
                    "path": str(destination),
                    "commit": commit,
                }
            )
            report["repos"].append(
                {
                    "name": name,
                    "source": source,
                    "path": str(destination),
                    "commit": commit,
                    "branch": _branch_name(state),
                    "dirty": bool(state.get("dirty")),
                    "reused_existing": True,
                }
            )
            return False
        elif reuse_existing and commit and head != commit:
            raise ReproductionError(
                f"Editable dependency path already exists at {destination}, "
                f"but HEAD is {head} instead of recorded commit {commit}. "
                "Use a workspace in a fresh parent directory or reproduce with "
                "a production Pixi environment."
            )
        elif not resume:
            raise ReproductionError(
                f"Repository destination already exists: {destination}"
            )
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        branch = _branch_name(state)
        clone_command = ["git", "clone"]
        if branch:
            clone_command.extend(["--branch", branch, "--single-branch"])
        clone_command.extend([source, str(destination)])
        _run(
            clone_command,
            report=report,
            step=f"clone {name}",
        )
    branch = _branch_name(state)
    if not branch:
        _run(
            ["git", "fetch", "--all", "--tags", "--prune"],
            cwd=destination,
            report=report,
            step=f"fetch {name}",
        )
    commit = state.get("commit")
    if commit:
        _run(
            ["git", "checkout", str(commit)],
            cwd=destination,
            report=report,
            step=f"checkout {name}",
        )
    report["repos"].append(
        {
            "name": name,
            "source": source,
            "path": str(destination),
            "commit": commit,
            "branch": branch,
            "dirty": bool(state.get("dirty")),
        }
    )
    return True


def _copy_artifact(
    source: Path | None,
    destination_dir: Path,
    report: dict[str, Any],
    *,
    name: str,
) -> Path | None:
    if source is None or not source.exists():
        return None
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name
    shutil.copy2(source, destination)
    report["artifacts"].append(
        {"name": name, "source": str(source), "path": str(destination)}
    )
    return destination


def _apply_patch_if_present(
    *,
    state: dict[str, Any],
    provenance_path: Path,
    repo_path: Path,
    artifact_dir: Path,
    report: dict[str, Any],
) -> None:
    patch = state.get("patch") or {}
    patch_path = _resolve_run_ref(provenance_path, patch.get("path"))
    if patch_path is None:
        return
    if not _validation(
        report,
        name=f"patch {_repo_name(state)}",
        path=patch_path,
        expected_sha256=patch.get("sha256"),
        required=True,
    ):
        return
    copied = _copy_artifact(
        patch_path, artifact_dir / "patches", report, name=f"patch {_repo_name(state)}"
    )
    if copied is not None:
        _run(
            ["git", "apply", str(copied)],
            cwd=repo_path,
            report=report,
            step=f"apply patch {_repo_name(state)}",
        )
        report["patches"].append(
            {
                "repo": _repo_name(state),
                "path": str(copied),
                "sha256": patch.get("sha256"),
            }
        )


def _environment_refs(
    record: dict[str, Any],
    provenance_path: Path,
) -> tuple[Path | None, Path | None, dict[str, Any]]:
    environment = record.get("environment") or {}
    lock_ref = environment.get("lockfile") or {}
    summary_ref = environment.get("summary") or {}
    lock_path = _resolve_run_ref(provenance_path, lock_ref.get("path"))
    summary_path = _resolve_run_ref(provenance_path, summary_ref.get("path"))
    return lock_path, summary_path, environment


def _load_environment_summary(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    return load_json(path)


def _verify_input_provenance(
    *,
    record: dict[str, Any],
    input_maps: dict[str, str],
    report: dict[str, Any],
) -> None:
    for index, item in enumerate(record.get("input_paths") or []):
        metadata = item.get("metadata") or {}
        product_provenance = metadata.get("product_provenance") or {}
        ref = product_provenance.get("path")
        expected = product_provenance.get("sha256")
        if not ref:
            continue
        original_input = str(item.get("path") or "")
        mapped_input = input_maps.get(original_input, original_input)
        sidecar = Path(mapped_input).parent / ref
        _validation(
            report,
            name=f"input product provenance {index}",
            path=sidecar,
            expected_sha256=expected,
            required=False,
        )


def _write_markdown_report(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Reproduction Report",
        "",
        f"- Status: `{report.get('status')}`",
        f"- Environment mode: `{report.get('environment', {}).get('mode', 'unknown')}`",
        f"- Workspace: `{report.get('workspace')}`",
        "",
        "## Command",
        "",
        "```bash",
        shlex.join(report.get("command", {}).get("effective", [])),
        "```",
        "",
        "## Warnings",
        "",
    ]
    warnings = report.get("warnings") or []
    lines.extend([f"- {item}" for item in warnings] or ["- None"])
    lines.extend(["", "## Blockers", ""])
    blockers = report.get("blockers") or []
    lines.extend([f"- {item}" for item in blockers] or ["- None"])
    lines.extend(["", "## Repositories", ""])
    repos = report.get("repos") or []
    lines.extend(
        [
            f"- {repo.get('name')}: `{repo.get('commit')}` at `{repo.get('path')}`"
            for repo in repos
        ]
        or ["- None"]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def reproduce_from_provenance(
    *,
    provenance: str | Path,
    workspace: str | Path,
    execute: bool = False,
    strict: bool = False,
    env: str | None = None,
    project_repo: str | None = None,
    repo_sources: dict[str, str] | None = None,
    input_maps: dict[str, str] | None = None,
    resume: bool = False,
    force: bool = False,
    install: bool = True,
) -> dict[str, Any]:
    """Create a reproduction workspace from a product provenance sidecar."""

    provenance_path = Path(provenance).resolve()
    workspace_path = Path(workspace).resolve()
    repo_sources = repo_sources or {}
    input_maps = input_maps or {}
    report: dict[str, Any] = {
        "schema_version": "1",
        "status": "started",
        "provenance": str(provenance_path),
        "workspace": str(workspace_path),
        "execute": execute,
        "strict": strict,
        "warnings": [],
        "blockers": [],
        "validations": [],
        "artifacts": [],
        "repos": [],
        "patches": [],
        "adaptations": [],
        "commands": [],
        "input_maps": input_maps,
    }

    record = load_json(provenance_path)
    report["product"] = record.get("product") or {}
    resolved_input_maps = resolve_input_maps(
        record=record,
        provenance_path=provenance_path,
        explicit_maps=input_maps,
        report=report,
    )
    report["input_maps"] = resolved_input_maps
    recorded_command = (
        record.get("command") or (record.get("process") or {}).get("command") or []
    )
    command = clean_command_parts(recorded_command)
    report["command"] = {
        "recorded": recorded_command,
        "cleaned": command,
        "effective": apply_input_maps(command, resolved_input_maps),
    }

    sidecar = provenance_path.with_suffix(f"{provenance_path.suffix}.sha256")
    if sidecar.exists():
        expected = _parse_sha256_sidecar(sidecar.read_text(encoding="utf-8"))
        _validation(
            report,
            name="product provenance",
            path=provenance_path,
            expected_sha256=expected,
            required=strict,
        )
    else:
        report["warnings"].append(
            f"Product provenance checksum sidecar is missing: {sidecar}"
        )

    lock_source, summary_source, environment_ref = _environment_refs(
        record, provenance_path
    )
    summary = _load_environment_summary(summary_source)
    pixi = summary.get("pixi") or {}
    env_name = (
        env
        or pixi.get("environment")
        or (summary.get("env_vars") or {}).get("PIXI_ENVIRONMENT_NAME")
    )
    if not env_name:
        raise ReproductionError("Pixi environment is not recorded; provide --env NAME.")

    lock_expected = (environment_ref.get("lockfile") or {}).get("sha256")
    if lock_source is not None:
        _validation(
            report,
            name="pixi lockfile",
            path=lock_source,
            expected_sha256=lock_expected,
            required=True,
        )
    else:
        raise ReproductionError("Provenance does not reference a pixi lockfile.")

    if summary_source is not None:
        _validation(
            report,
            name="environment summary",
            path=summary_source,
            expected_sha256=(environment_ref.get("summary") or {}).get("sha256"),
            required=False,
        )

    lock_text = lock_source.read_text(encoding="utf-8") if lock_source else ""
    local_paths = pixi.get("local_path_dependencies")
    if local_paths is None:
        local_paths = pixi_local_path_dependencies(lock_text, env_name)
    editable = bool(pixi.get("editable_dependencies", bool(local_paths)))
    report["environment"] = {
        "name": env_name,
        "mode": "editable-local" if editable else "production",
        "editable_dependencies": editable,
        "local_path_dependencies": local_paths,
    }

    _verify_input_provenance(
        record=record,
        input_maps=resolved_input_maps,
        report=report,
    )

    if workspace_path.exists():
        if force:
            shutil.rmtree(workspace_path)
        elif not resume:
            raise ReproductionError(
                f"Workspace already exists: {workspace_path}. Use --resume or --force."
            )

    repos = record.get("software_repos") or []
    project = _select_project_repo(repos, project_repo)
    repo_root = workspace_path / "repos"
    project_path = repo_root / _repo_name(project)
    report["project_repo_path"] = str(project_path)
    artifact_dir = workspace_path / "provenance-source"
    project_was_restored = _clone_or_resume_repo(
        state=project,
        destination=project_path,
        sources=repo_sources,
        report=report,
        resume=resume,
    )
    if project_was_restored:
        _apply_patch_if_present(
            state=project,
            provenance_path=provenance_path,
            repo_path=project_path,
            artifact_dir=artifact_dir,
            report=report,
        )
    _copy_artifact(provenance_path, artifact_dir, report, name="product provenance")
    _copy_artifact(
        sidecar if sidecar.exists() else None,
        artifact_dir,
        report,
        name="product provenance checksum",
    )
    copied_lock = _copy_artifact(
        lock_source,
        artifact_dir / "environment",
        report,
        name="pixi lockfile",
    )
    _copy_artifact(
        summary_source,
        artifact_dir / "environment",
        report,
        name="environment summary",
    )
    if copied_lock is not None:
        shutil.copy2(copied_lock, project_path / "pixi.lock")

    repo_by_name = {_repo_name(state): state for state in repos}
    if editable:
        for local_path in local_paths:
            if not str(local_path).startswith("../"):
                continue
            name = Path(local_path).name
            state = repo_by_name.get(name)
            if state is None:
                state = _local_repo_state(name)
                if state is None:
                    report["blockers"].append(
                        f"Editable dependency {name!r} not found in software_repos "
                        "and no matching local checkout was found."
                    )
                    continue
                report["warnings"].append(
                    f"Editable dependency {name!r} was not tracked in "
                    "software_repos; using matching local checkout."
                )
            destination = (project_path / str(local_path)).resolve()
            dependency_was_restored = _clone_or_resume_repo(
                state=state,
                destination=destination,
                sources=repo_sources,
                report=report,
                resume=resume,
                reuse_existing=True,
            )
            if dependency_was_restored:
                _apply_patch_if_present(
                    state=state,
                    provenance_path=provenance_path,
                    repo_path=destination,
                    artifact_dir=artifact_dir,
                    report=report,
                )

    try:
        if strict and (report["warnings"] or report["blockers"]):
            report["status"] = "failed_strict"
        elif report["blockers"]:
            report["status"] = "blocked"
        else:
            if install:
                _run(
                    ["pixi", "install", "--locked", "-e", str(env_name)],
                    cwd=project_path,
                    report=report,
                    step="pixi install",
                )
            if execute:
                proc = _run(
                    [
                        "pixi",
                        "run",
                        "-e",
                        str(env_name),
                        *report["command"]["effective"],
                    ],
                    cwd=project_path,
                    report=report,
                    step="execute command",
                )
                report["execution"] = {"returncode": proc.returncode}
            report["status"] = "completed"
    except Exception as err:
        report["status"] = "failed"
        report["blockers"].append(str(err))
        write_json(workspace_path / "reproduction.json", report)
        _write_markdown_report(workspace_path / "REPRODUCTION.md", report)
        raise

    write_json(workspace_path / "reproduction.json", report)
    _write_markdown_report(workspace_path / "REPRODUCTION.md", report)
    return report
