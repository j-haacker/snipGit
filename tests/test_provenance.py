from __future__ import annotations

import subprocess
from datetime import datetime, timezone

import pytest

from snippets.provenance import (
    append_cf_history,
    append_xarray_history,
    build_cf_history_entry,
    canonicalize_remote_url,
    enforce_clean_repos,
    get_git_state,
    get_input_path_state,
    public_git_state,
)


def _run(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init"], repo)
    _run(["git", "config", "user.email", "test@example.invalid"], repo)
    _run(["git", "config", "user.name", "Test User"], repo)
    (repo / "data.txt").write_text("clean\n", encoding="utf-8")
    _run(["git", "add", "data.txt"], repo)
    _run(["git", "commit", "-m", "initial"], repo)
    return repo


def test_get_git_state_clean_and_dirty(tmp_path):
    repo = _repo(tmp_path)

    clean = get_git_state(repo)
    assert clean.commit
    assert not clean.dirty
    assert clean.diff_hash is None

    (repo / "data.txt").write_text("dirty\n", encoding="utf-8")
    dirty = get_git_state(repo)
    assert dirty.dirty
    assert dirty.dirty_marker == "+dirty"
    assert dirty.diff_hash
    assert "data.txt" in dirty.status_short


def test_get_git_state_detached_head(tmp_path):
    repo = _repo(tmp_path)
    commit = get_git_state(repo).commit
    _run(["git", "checkout", "--detach", commit], repo)

    state = get_git_state(repo)
    assert state.branch is None


def test_enforce_clean_repos_requires_allow_dirty(tmp_path):
    repo = _repo(tmp_path)
    (repo / "data.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="--allow-dirty"):
        enforce_clean_repos([repo])

    states = enforce_clean_repos([repo], allow_dirty=True)
    assert states[0].dirty


def test_history_append_prepends_new_entry():
    assert append_cf_history("old", "new") == "new\nold"


def test_canonical_remote_and_public_git_state_omit_local_root(tmp_path):
    repo = _repo(tmp_path)
    _run(["git", "remote", "add", "origin", "github:j-haacker/snipGit"], repo)

    state = public_git_state(get_git_state(repo))

    assert state["remote_url"] == "https://github.com/j-haacker/snipGit"
    assert canonicalize_remote_url("ssh://github/j-haacker/snipGit.git") == "https://github.com/j-haacker/snipGit"
    assert state["name"] == "snipGit"
    assert "repo_root" not in state
    assert "diff_hash" not in state


def test_history_entry_normalizes_module_command_and_omits_inputs_by_default():
    entry = build_cf_history_entry(
        [
            "/home/user/c4v-utils/src/c4v_utils/downscaling.py",
            "apply",
            "--provenance-json",
            "run.provenance.json",
        ],
        input_states=[{"path": "/tmp/input.zarr", "backend": "filesystem"}],
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert "python -m c4v_utils.downscaling apply" in entry
    assert "--provenance-json" not in entry
    assert "inputs=" not in entry


def test_xarray_history_attr_roundtrip():
    xr = pytest.importorskip("xarray")
    ds = xr.Dataset(attrs={"history": "old"})

    out = append_xarray_history(ds, "new", copy=True)

    assert out.attrs["history"] == "new\nold"
    assert ds.attrs["history"] == "old"


def test_synthetic_lfs_pointer_is_detected(tmp_path):
    pointer = tmp_path / "data.nc"
    pointer.write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:0123456789abcdef\n"
        "size 123\n",
        encoding="utf-8",
    )

    state = get_input_path_state(pointer)
    assert state.backend == "git-lfs"
    assert state.metadata["lfs"]["oid"] == "0123456789abcdef"


def test_synthetic_dvc_file_is_detected(tmp_path):
    repo = _repo(tmp_path)
    (repo / "data.bin.dvc").write_text(
        "outs:\n"
        "- md5: abc123\n"
        "  size: 9\n"
        "  path: data.bin\n",
        encoding="utf-8",
    )
    _run(["git", "add", "data.bin.dvc"], repo)
    _run(["git", "commit", "-m", "track dvc"], repo)

    state = get_input_path_state(repo / "data.bin")
    assert state.backend == "dvc"
    assert state.metadata["dvc"]["outputs"][0]["md5"] == "abc123"
