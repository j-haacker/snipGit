__all__ = [
    "append_git_state_snapshot",
    "git_state_context",
    "mail_traceback_wrapper",
    "pipe_output_and_git_state_log",
    "pipe_output_to_logfile",
]

from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
import inspect
import smtplib
import subprocess
import sys
import traceback
from typing import Literal

try:
    from snippets.provenance import run_git as _shared_run_git
except Exception:
    _shared_run_git = None


def mail_traceback_wrapper(
    to_address: str, subject_tag: str = None, from_address: str = None
):
    # CREDIT: FBruzzesi, Nathan Davis https://stackoverflow.com/a/27500036
    def decorate(f):
        def applicator(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except Exception as err:
                msg = EmailMessage()
                msg["To"] = to_address
                subject = str(err).strip().replace("\n", "; ")
                if subject_tag is not None and subject_tag != "":
                    subject = f"[{subject_tag.strip()}] {subject}"
                msg["Subject"] = subject
                if from_address is not None:
                    msg["From"] = from_address.replace(" ", "_")
                msg.set_content(traceback.format_exc())
                print(str(err).strip().replace("\n", "; "), msg)
                with smtplib.SMTP("localhost") as s:
                    s.send_message(msg)
                raise

        return applicator

    return decorate


@contextmanager
def pipe_output_to_logfile(path: Path | str, mode: Literal["a", "w"]):
    with open(path, mode) as lf:
        orig_pipes = sys.stderr, sys.stdout
        sys.stderr = sys.stdout = lf
        try:
            yield
        finally:
            sys.stderr, sys.stdout = orig_pipes


def _run_git(args: Sequence[str], cwd: Path) -> tuple[bool, str, str]:
    if _shared_run_git is not None:
        return _shared_run_git(args, cwd)
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
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
        if error == "":
            error = f"git {' '.join(args)} failed with exit code {proc.returncode}"
        return False, "", error
    return True, proc.stdout, ""


def _iter_parent_dirs(start: Path, max_parent_levels: int) -> list[Path]:
    levels = max(0, max_parent_levels)
    dirs = [start]
    current = start
    for _ in range(levels):
        parent = current.parent
        if parent == current:
            break
        dirs.append(parent)
        current = parent
    return dirs


def _infer_caller_file(max_parent_levels: int) -> Path | None:
    module_file = Path(__file__).resolve()
    search_roots = [
        path
        for path in _iter_parent_dirs(Path.cwd().resolve(), max_parent_levels)
        if path.parent != path
    ]
    stack = inspect.stack()
    try:
        for frame_info in stack:
            filename = frame_info.filename
            if filename.startswith("<"):
                continue
            try:
                candidate = Path(filename).resolve()
            except OSError:
                continue
            if candidate == module_file:
                continue
            if not candidate.exists():
                continue
            if any(
                candidate == root or root in candidate.parents
                for root in search_roots
            ):
                return candidate
    finally:
        del stack
    return None


def _dedupe_paths(paths: Sequence[Path]) -> list[Path]:
    unique_paths = []
    seen = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique_paths.append(path)
    return unique_paths


def _default_anchor_paths(max_parent_levels: int) -> list[Path]:
    anchors = []
    caller_file = _infer_caller_file(max_parent_levels=max_parent_levels)
    if caller_file is not None:
        anchors.append(caller_file)
    for parent_dir in _iter_parent_dirs(Path.cwd().resolve(), max_parent_levels):
        snakefile = parent_dir / "Snakefile"
        if snakefile.exists():
            anchors.append(snakefile.resolve())
    return _dedupe_paths(anchors)


def _pathspec_candidates(anchor_path: Path, repo_root: Path) -> list[str]:
    pathspecs = []
    if anchor_path.is_absolute():
        try:
            rel = anchor_path.resolve().relative_to(repo_root)
        except (OSError, ValueError):
            return []
        pathspecs.append(rel.as_posix())
        return pathspecs

    pathspecs.append(anchor_path.as_posix())
    try:
        rel_from_cwd = (Path.cwd() / anchor_path).resolve().relative_to(repo_root)
    except (OSError, ValueError):
        rel_from_cwd = None
    if rel_from_cwd is not None:
        rel_from_cwd_str = rel_from_cwd.as_posix()
        if rel_from_cwd_str not in pathspecs:
            pathspecs.append(rel_from_cwd_str)
    return pathspecs


def _has_tracked_anchor(repo_root: Path, anchors: Sequence[Path]) -> bool:
    for anchor_path in anchors:
        for pathspec in _pathspec_candidates(anchor_path, repo_root):
            ok, _, _ = _run_git(
                ["ls-files", "--error-unmatch", "--", pathspec], cwd=repo_root
            )
            if ok:
                return True
    return False


def _discover_repo_root(
    repo_dir: Path | str | None,
    anchor_paths: Sequence[Path | str] | None,
    max_parent_levels: int,
) -> tuple[Path | None, list[str], list[Path]]:
    warnings = []
    if anchor_paths is None:
        anchors = _default_anchor_paths(max_parent_levels=max_parent_levels)
    else:
        anchors = _dedupe_paths([Path(anchor).expanduser() for anchor in anchor_paths])

    if repo_dir is not None:
        candidate_dirs = [Path(repo_dir).expanduser().resolve()]
    else:
        candidate_dirs = _iter_parent_dirs(
            start=Path.cwd().resolve(), max_parent_levels=max_parent_levels
        )

    for candidate_dir in candidate_dirs:
        ok, output, error = _run_git(
            ["rev-parse", "--show-toplevel"], cwd=candidate_dir
        )
        if not ok:
            if repo_dir is not None:
                warnings.append(
                    f"Could not resolve git repo from {candidate_dir}: {error}"
                )
            continue
        repo_root = Path(output.strip()).resolve()
        if anchors and not _has_tracked_anchor(repo_root=repo_root, anchors=anchors):
            warnings.append(
                "Anchor check failed for candidate "
                f"{repo_root}; none of the anchors appear to be tracked there."
            )
            continue
        return repo_root, warnings, anchors

    if repo_dir is None:
        warnings.append(
            "Could not find a git repository in current directory or configured "
            f"parents (max_parent_levels={max(0, max_parent_levels)})."
        )
    return None, warnings, anchors


def _render_or_empty(text: str) -> str:
    if text == "":
        return "(empty)\n"
    if text.endswith("\n"):
        return text
    return f"{text}\n"


def _append_reflog_activity_warning(
    log_path: Path | str,
    *,
    run_start: datetime,
    repo_dir: Path | str | None,
    anchor_paths: Sequence[Path | str] | None,
    max_parent_levels: int,
    encoding: str,
) -> None:
    """Append a warning if reflog shows HEAD activity since `run_start`."""
    repo_root, _, _ = _discover_repo_root(
        repo_dir=repo_dir,
        anchor_paths=anchor_paths,
        max_parent_levels=max_parent_levels,
    )
    if repo_root is None:
        return

    run_start_str = run_start.isoformat(timespec="seconds")
    reflog_ok, reflog_output, _ = _run_git(
        ["reflog", "--since", run_start_str, "--date=iso-strict"],
        cwd=repo_root,
    )
    if not reflog_ok:
        return

    reflog_lines = [line for line in reflog_output.splitlines() if line.strip() != ""]
    if len(reflog_lines) == 0:
        return

    log_file = Path(log_path).expanduser()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding=encoding) as handle:
        handle.write(
            "WARNING: Git reflog recorded "
            f"{len(reflog_lines)} action(s) since run start {run_start_str}. "
            "Repository state may have changed during execution.\n\n"
        )


def append_git_state_snapshot(
    log_path: Path | str,
    *,
    section: Literal["START", "END", "POINT"] = "POINT",
    repo_dir: Path | str | None = None,
    anchor_paths: Sequence[Path | str] | None = None,
    max_parent_levels: int = 3,
    max_diff_bytes: int = 262_144,
    encoding: str = "utf-8",
) -> None:
    """Append git state metadata to a plain-text logfile.

    Args:
        log_path (Path | str): Log file target.
        section (Literal["START", "END", "POINT"], optional): Snapshot label.
        repo_dir (Path | str | None, optional): Directory to start git discovery from.
        anchor_paths (Sequence[Path | str] | None, optional): Paths expected to be
            tracked in the target repository.
        max_parent_levels (int, optional): Number of parent directories to inspect
            when `repo_dir` is omitted.
        max_diff_bytes (int, optional): If staged + unstaged patch size is larger than
            this threshold, the function logs `--stat` output instead of full patch
            output.
        encoding (str, optional): File encoding used for writing and size accounting.
    """
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log_file = Path(log_path).expanduser()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    repo_root, warnings, anchors = _discover_repo_root(
        repo_dir=repo_dir,
        anchor_paths=anchor_paths,
        max_parent_levels=max_parent_levels,
    )

    with log_file.open("a", encoding=encoding) as handle:
        handle.write(
            f"===== GIT STATE SNAPSHOT [{section}] {timestamp} =====\n"
        )
        if anchors:
            anchor_text = ", ".join(str(anchor) for anchor in anchors)
            handle.write(f"anchor_paths: {anchor_text}\n")
        else:
            handle.write("anchor_paths: (none)\n")
        for warning in warnings:
            handle.write(f"WARNING: {warning}\n")

        if repo_root is None:
            handle.write(
                "WARNING: Snapshot skipped because no valid git repository "
                "could be resolved.\n"
            )
            handle.write("===== END GIT STATE SNAPSHOT =====\n\n")
            return

        head_ok, head_output, head_error = _run_git(
            ["rev-parse", "HEAD"], cwd=repo_root
        )
        branch_ok, branch_output, branch_error = _run_git(
            ["branch", "--show-current"], cwd=repo_root
        )
        staged_ok, staged_patch, staged_error = _run_git(
            ["diff", "--cached", "--no-ext-diff", "--"], cwd=repo_root
        )
        unstaged_ok, unstaged_patch, unstaged_error = _run_git(
            ["diff", "--no-ext-diff", "--"], cwd=repo_root
        )

        if not head_ok:
            handle.write(f"WARNING: Failed to read HEAD commit: {head_error}\n")
        if not branch_ok:
            handle.write(f"WARNING: Failed to read branch name: {branch_error}\n")
        if not staged_ok:
            handle.write(f"WARNING: Failed to read staged diff: {staged_error}\n")
        if not unstaged_ok:
            handle.write(f"WARNING: Failed to read unstaged diff: {unstaged_error}\n")

        combined_patch = f"{staged_patch}{unstaged_patch}"
        patch_bytes = len(combined_patch.encode(encoding, errors="replace"))
        diff_mode = "patch"
        staged_output = staged_patch
        unstaged_output = unstaged_patch

        if patch_bytes > max_diff_bytes:
            diff_mode = "stat_fallback"
            handle.write(
                "WARNING: Combined patch size exceeded threshold "
                f"({patch_bytes} > {max_diff_bytes}); writing --stat output.\n"
            )
            staged_stat_ok, staged_stat, staged_stat_error = _run_git(
                ["diff", "--cached", "--no-ext-diff", "--stat", "--"], cwd=repo_root
            )
            unstaged_stat_ok, unstaged_stat, unstaged_stat_error = _run_git(
                ["diff", "--no-ext-diff", "--stat", "--"], cwd=repo_root
            )
            if staged_stat_ok:
                staged_output = staged_stat
            else:
                handle.write(
                    "WARNING: Failed to read staged --stat diff: "
                    f"{staged_stat_error}\n"
                )
            if unstaged_stat_ok:
                unstaged_output = unstaged_stat
            else:
                handle.write(
                    "WARNING: Failed to read unstaged --stat diff: "
                    f"{unstaged_stat_error}\n"
                )

        branch_name = branch_output.strip() if branch_ok else ""
        branch_value = branch_name if branch_name != "" else "(detached-or-unknown)"
        head_value = head_output.strip() if head_ok else "(unknown)"
        dirty = (
            (staged_patch != "" if staged_ok else False)
            or (unstaged_patch != "" if unstaged_ok else False)
        )

        handle.write(f"repo_root: {repo_root}\n")
        handle.write(f"head: {head_value}\n")
        handle.write(f"branch: {branch_value}\n")
        handle.write(f"dirty: {dirty}\n")
        handle.write(
            f"diff_mode: {diff_mode}; max_diff_bytes: {max_diff_bytes}; "
            f"measured_patch_bytes: {patch_bytes}\n"
        )
        handle.write("--- STAGED DIFF START ---\n")
        handle.write(_render_or_empty(staged_output))
        handle.write("--- STAGED DIFF END ---\n")
        handle.write("--- UNSTAGED DIFF START ---\n")
        handle.write(_render_or_empty(unstaged_output))
        handle.write("--- UNSTAGED DIFF END ---\n")
        handle.write("===== END GIT STATE SNAPSHOT =====\n\n")


@contextmanager
def git_state_context(
    log_path: Path | str,
    *,
    snapshot_timing: Literal["start", "end", "both"] = "both",
    repo_dir: Path | str | None = None,
    anchor_paths: Sequence[Path | str] | None = None,
    max_parent_levels: int = 3,
    max_diff_bytes: int = 262_144,
    encoding: str = "utf-8",
):
    """Context manager that writes START/END git state snapshots.

    Example:
        with git_state_context("run.log", snapshot_timing="both"):
            run_workflow()
    """
    if snapshot_timing not in {"start", "end", "both"}:
        raise ValueError(
            "snapshot_timing must be one of 'start', 'end', or 'both', got "
            f"{snapshot_timing!r}."
        )

    run_start = datetime.now(timezone.utc)

    if snapshot_timing in {"start", "both"}:
        append_git_state_snapshot(
            log_path=log_path,
            section="START",
            repo_dir=repo_dir,
            anchor_paths=anchor_paths,
            max_parent_levels=max_parent_levels,
            max_diff_bytes=max_diff_bytes,
            encoding=encoding,
        )

    try:
        yield
    finally:
        if snapshot_timing in {"end", "both"}:
            _append_reflog_activity_warning(
                log_path=log_path,
                run_start=run_start,
                repo_dir=repo_dir,
                anchor_paths=anchor_paths,
                max_parent_levels=max_parent_levels,
                encoding=encoding,
            )
            append_git_state_snapshot(
                log_path=log_path,
                section="END",
                repo_dir=repo_dir,
                anchor_paths=anchor_paths,
                max_parent_levels=max_parent_levels,
                max_diff_bytes=max_diff_bytes,
                encoding=encoding,
            )


@contextmanager
def pipe_output_and_git_state_log(
    log_path: Path | str,
    mode: Literal["a", "w"] = "a",
    *,
    snapshot_timing: Literal["start", "end", "both"] = "both",
    repo_dir: Path | str | None = None,
    anchor_paths: Sequence[Path | str] | None = None,
    max_parent_levels: int = 3,
    max_diff_bytes: int = 262_144,
    encoding: str = "utf-8",
):
    """Pipe stdout/stderr and log git state snapshots in one context.

    Example:
        with pipe_output_and_git_state_log("run.log", mode="w"):
            print("workflow started")
            run_workflow()
    """
    if mode not in {"a", "w"}:
        raise ValueError(f"mode must be 'a' or 'w', got {mode!r}.")

    log_file = Path(log_path).expanduser()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    if mode == "w":
        log_file.write_text("", encoding=encoding)

    with git_state_context(
        log_path=log_file,
        snapshot_timing=snapshot_timing,
        repo_dir=repo_dir,
        anchor_paths=anchor_paths,
        max_parent_levels=max_parent_levels,
        max_diff_bytes=max_diff_bytes,
        encoding=encoding,
    ):
        with pipe_output_to_logfile(log_file, "a"):
            yield
