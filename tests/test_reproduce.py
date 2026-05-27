from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from snippets.reproduce import ReproductionError, reproduce_from_provenance


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _git_repo(
    tmp_path: Path, name: str, *, files: dict[str, str] | None = None
) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _run(["git", "init"], repo)
    _run(["git", "config", "user.email", "test@example.invalid"], repo)
    _run(["git", "config", "user.name", "Test User"], repo)
    for path, text in (files or {"README.md": name}).items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-m", "initial"], repo)
    _run(["git", "checkout", "-b", "dev"], repo)
    (repo / ".branch").write_text("dev\n", encoding="utf-8")
    _run(["git", "add", ".branch"], repo)
    _run(["git", "commit", "-m", "dev"], repo)
    return repo


def _commit(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _branch(repo: Path) -> str:
    return subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_provenance(
    run_root: Path,
    *,
    main_repo: Path,
    env_name: str,
    lock_text: str,
    editable: bool = False,
    dep_repo: Path | None = None,
    dep_path: str = "../dep",
    legacy_editable: bool = False,
) -> Path:
    env_dir = run_root / "provenance" / "environment"
    product_dir = run_root / "factors" / "hurs"
    env_dir.mkdir(parents=True)
    product_dir.mkdir(parents=True)
    lock = env_dir / "pixi.lock"
    summary = env_dir / "environment.json"
    lock.write_text(lock_text, encoding="utf-8")
    local_paths = [dep_path] if editable else []
    pixi = {
        "environment": env_name,
        "editable_dependencies": editable,
        "local_path_dependencies": local_paths,
    }
    if editable and not legacy_editable:
        dependency = {
            "path": dep_path,
            "package": "dep",
            "repo": "dep",
            "kind": "external-editable",
            "local": True,
            "editable": True,
        }
        pixi["local_dependencies"] = [dependency]
        pixi["external_editable_dependencies"] = [dependency]
    summary_payload = {"pixi": pixi}
    if not legacy_editable:
        summary_payload["schema_version"] = "2"
    summary.write_text(
        json.dumps(summary_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    repos = [
        {
            "name": "main",
            "commit": _commit(main_repo),
            "branch": "dev",
            "remote_url": str(main_repo),
            "dirty": False,
        }
    ]
    if dep_repo is not None:
        repos.append(
            {
                "name": "dep",
                "commit": _commit(dep_repo),
                "branch": "dev",
                "remote_url": str(dep_repo),
                "dirty": False,
            }
        )
    provenance = {
        "command": [
            "python",
            "-m",
            "c4v_utils.downscaling",
            "train",
            "--input",
            "old-input.zarr",
            "--provenance-json",
            "hurs.prov.json",
        ],
        "environment": {
            "lockfile": {
                "path": "provenance/environment/pixi.lock",
                "sha256": _sha(lock),
            },
            "summary": {
                "path": "provenance/environment/environment.json",
                "sha256": _sha(summary),
            },
        },
        "product": {"data": "hurs.zarr"},
        "software_repos": repos,
    }
    path = product_dir / "hurs.prov.json"
    path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    digest = _sha(path)
    (product_dir / "hurs.prov.json.sha256").write_text(
        f"{digest}  hurs.prov.json\n", encoding="utf-8"
    )
    return path


def test_reproduce_sets_up_production_workspace_without_editable_deps(tmp_path):
    main_repo = _git_repo(
        tmp_path,
        "main",
        files={"pyproject.toml": "[tool.pixi.workspace]\n"},
    )
    lock_text = (
        "version: 6\n"
        "environments:\n"
        "  downscale:\n"
        "    packages:\n"
        "      linux-64:\n"
        "      - pypi: git+ssh://example/repo.git#abc\n"
    )
    provenance = _write_provenance(
        tmp_path / "run",
        main_repo=main_repo,
        env_name="downscale",
        lock_text=lock_text,
    )

    report = reproduce_from_provenance(
        provenance=provenance,
        workspace=tmp_path / "workspace",
        install=False,
    )

    assert report["status"] == "completed"
    assert report["environment"]["mode"] == "production"
    assert (tmp_path / "workspace" / ".git").exists()
    assert (tmp_path / "workspace" / "pixi.lock").read_text() == (
        tmp_path / "run" / "provenance" / "environment" / "pixi.lock"
    ).read_text()
    assert "repos/" in (
        tmp_path / "workspace" / ".git" / "info" / "exclude"
    ).read_text()
    assert "--provenance-json" not in report["command"]["effective"]
    assert (tmp_path / "workspace" / "reproduction.json").exists()
    assert (tmp_path / "workspace" / "REPRODUCTION.md").exists()
    assert any(
        item["step"] == "clone main"
        and item["command"][:4] == ["git", "clone", "--branch", "dev"]
        for item in report["commands"]
    )
    assert not any(item["step"] == "fetch main branch" for item in report["commands"])


def test_reproduce_prefers_matching_local_checkout(tmp_path, monkeypatch):
    main_repo = _git_repo(
        tmp_path,
        "main",
        files={"pyproject.toml": "[tool.pixi.workspace]\n"},
    )
    provenance = _write_provenance(
        tmp_path / "run",
        main_repo=main_repo,
        env_name="downscale",
        lock_text="version: 6\nenvironments:\n  downscale:\n    packages: {}\n",
    )
    payload = json.loads(provenance.read_text(encoding="utf-8"))
    payload["software_repos"][0]["remote_url"] = "https://example.invalid/main"
    provenance.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    monkeypatch.chdir(main_repo)

    report = reproduce_from_provenance(
        provenance=provenance,
        workspace=tmp_path / "workspace",
        install=False,
    )

    assert report["status"] == "completed"
    assert report["repos"][0]["source"] == str(main_repo)


def test_reproduce_resolves_recorded_input_paths(tmp_path, monkeypatch):
    main_repo = _git_repo(
        tmp_path,
        "main",
        files={"pyproject.toml": "[tool.pixi.workspace]\n"},
    )
    input_path = tmp_path / "source-data" / "old-input.zarr"
    input_path.parent.mkdir()
    input_path.write_text("data\n", encoding="utf-8")
    provenance = _write_provenance(
        tmp_path / "run",
        main_repo=main_repo,
        env_name="downscale",
        lock_text="version: 6\nenvironments:\n  downscale:\n    packages: {}\n",
    )
    payload = json.loads(provenance.read_text(encoding="utf-8"))
    payload["command"] = [
        "python",
        "-m",
        "example",
        "--input",
        "source-data/old-input.zarr",
    ]
    payload["input_paths"] = [{"path": "source-data/old-input.zarr"}]
    provenance.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    monkeypatch.chdir(tmp_path)

    report = reproduce_from_provenance(
        provenance=provenance,
        workspace=tmp_path / "workspace",
        install=False,
    )

    assert report["status"] == "completed"
    assert report["command"]["effective"][-1] == str(input_path)
    assert report["resolved_inputs"] == [
        {
            "path": "source-data/old-input.zarr",
            "resolved": str(input_path),
            "source": "recorded-input-path",
        }
    ]


def test_reproduce_preserves_editable_dependency_paths(tmp_path):
    main_repo = _git_repo(
        tmp_path,
        "main",
        files={
            "pyproject.toml": (
                "[tool.pixi.feature.utils-local.pypi-dependencies]\n"
                'dep = { path = "../dep", editable = true }\n'
            )
        },
    )
    dep_repo = _git_repo(tmp_path, "dep")
    lock_text = (
        "version: 6\n"
        "environments:\n"
        "  downscale-local:\n"
        "    packages:\n"
        "      linux-64:\n"
        "      - pypi: ../dep\n"
    )
    provenance = _write_provenance(
        tmp_path / "run",
        main_repo=main_repo,
        dep_repo=dep_repo,
        env_name="downscale-local",
        editable=True,
        lock_text=lock_text,
    )

    report = reproduce_from_provenance(
        provenance=provenance,
        workspace=tmp_path / "workspace",
        install=False,
    )

    assert report["status"] == "completed"
    assert report["environment"]["mode"] == "editable-local"
    assert (tmp_path / "workspace" / "repos" / "dep" / ".git").exists()
    pyproject_text = (tmp_path / "workspace" / "pyproject.toml").read_text()
    assert 'path = "repos/dep"' in pyproject_text
    assert "- pypi: repos/dep" in (tmp_path / "workspace" / "pixi.lock").read_text()
    assert _branch(dep_repo) == "dev"
    assert not any(item["cwd"] == str(dep_repo) for item in report["commands"])
    assert any(
        item["kind"] == "editable-path-rewrite" for item in report["adaptations"]
    )


def test_reproduce_blocks_missing_editable_dependency_provenance(tmp_path, monkeypatch):
    main_repo = _git_repo(
        tmp_path,
        "main",
        files={
            "pyproject.toml": (
                "[tool.pixi.feature.utils-local.pypi-dependencies]\n"
                'dep = { path = "../dep", editable = true }\n'
            )
        },
    )
    dep_repo = _git_repo(tmp_path, "dep")
    provenance = _write_provenance(
        tmp_path / "run",
        main_repo=main_repo,
        env_name="downscale-local",
        editable=True,
        lock_text=(
            "version: 6\n"
            "environments:\n"
            "  downscale-local:\n"
            "    packages:\n"
            "      linux-64:\n"
            "      - pypi: ../dep\n"
        ),
    )
    monkeypatch.chdir(main_repo)

    report = reproduce_from_provenance(
        provenance=provenance,
        workspace=tmp_path / "workspace",
        install=False,
    )

    assert report["status"] == "blocked"
    assert not (tmp_path / "workspace" / "repos" / "dep" / ".git").exists()
    assert report["blockers"] == [
        "Editable dependency 'dep' is missing from software_repos."
    ]


def test_reproduce_rewrites_absolute_editable_dependency_path(tmp_path):
    dep_repo = _git_repo(tmp_path, "dep")
    main_repo = _git_repo(
        tmp_path,
        "main",
        files={
            "pyproject.toml": (
                "[tool.pixi.feature.utils-local.pypi-dependencies]\n"
                f'dep = {{ path = "{dep_repo}", editable = true }}\n'
            )
        },
    )
    lock_text = (
        "version: 6\n"
        "environments:\n"
        "  downscale-local:\n"
        "    packages:\n"
        "      linux-64:\n"
        f"      - pypi: {dep_repo}\n"
    )
    provenance = _write_provenance(
        tmp_path / "run",
        main_repo=main_repo,
        dep_repo=dep_repo,
        env_name="downscale-local",
        editable=True,
        dep_path=str(dep_repo),
        lock_text=lock_text,
    )

    report = reproduce_from_provenance(
        provenance=provenance,
        workspace=tmp_path / "workspace",
        install=False,
    )

    assert report["status"] == "completed"
    assert f'path = "{dep_repo}"' not in (
        tmp_path / "workspace" / "pyproject.toml"
    ).read_text()
    assert 'path = "repos/dep"' in (tmp_path / "workspace" / "pyproject.toml").read_text()
    assert f"- pypi: {dep_repo}" not in (tmp_path / "workspace" / "pixi.lock").read_text()
    assert "- pypi: repos/dep" in (tmp_path / "workspace" / "pixi.lock").read_text()


def test_reproduce_rejects_legacy_editable_environment_schema(tmp_path):
    main_repo = _git_repo(tmp_path, "main")
    dep_repo = _git_repo(tmp_path, "dep")
    provenance = _write_provenance(
        tmp_path / "run",
        main_repo=main_repo,
        dep_repo=dep_repo,
        env_name="downscale-local",
        editable=True,
        legacy_editable=True,
        lock_text=(
            "version: 6\n"
            "environments:\n"
            "  downscale-local:\n"
            "    packages:\n"
            "      linux-64:\n"
            "      - pypi: ../dep\n"
        ),
    )

    with pytest.raises(ReproductionError, match="environment schema v2"):
        reproduce_from_provenance(
            provenance=provenance,
            workspace=tmp_path / "workspace",
            install=False,
        )


def test_reproduce_existing_workspace_requires_resume_or_force(tmp_path):
    main_repo = _git_repo(tmp_path, "main")
    provenance = _write_provenance(
        tmp_path / "run",
        main_repo=main_repo,
        env_name="downscale",
        lock_text="version: 6\nenvironments:\n  downscale:\n    packages: {}\n",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ReproductionError, match="Workspace already exists"):
        reproduce_from_provenance(
            provenance=provenance,
            workspace=workspace,
            install=False,
        )
