from __future__ import annotations

import subprocess

import pytest

from snippets.provenance import (
    append_cf_history,
    append_xarray_history,
    enforce_clean_repos,
    get_git_state,
    get_input_path_state,
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

    (repo / "data.txt").write_text("dirty\n", encoding="utf-8")
    dirty = get_git_state(repo)
    assert dirty.dirty
    assert dirty.dirty_marker == "+dirty"
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
