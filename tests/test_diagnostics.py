from snippets.debugging import Diagnostics, NO_DIAGNOSTICS


def test_disabled_diagnostics_do_not_create_output_directories(tmp_path):
    calls = []
    outdir = tmp_path / "debug"
    diagnostics = Diagnostics(enabled=False, outdir=outdir)

    result = diagnostics.emit(
        "input", lambda path: calls.append(path) or (path / "input.txt").write_text("x")
    )

    assert result is None
    assert calls == []
    assert not outdir.exists()


def test_enabled_diagnostics_call_function_and_create_directory(tmp_path):
    diagnostics = Diagnostics(enabled=True, outdir=tmp_path / "debug")

    result = diagnostics.emit(
        "output", lambda path: (path / "output.txt").write_text("ok")
    )

    assert result == 2
    assert (tmp_path / "debug" / "output" / "output.txt").read_text() == "ok"


def test_only_filters_events(tmp_path):
    diagnostics = Diagnostics(
        enabled=True,
        outdir=tmp_path / "debug",
        only={"keep"},
    )

    diagnostics.emit("skip", lambda path: (path / "skip.txt").write_text("no"))
    diagnostics.emit("keep", lambda path: (path / "keep.txt").write_text("yes"))

    assert not (tmp_path / "debug" / "skip").exists()
    assert (tmp_path / "debug" / "keep" / "keep.txt").read_text() == "yes"


def test_default_no_diagnostics_works_without_setup(tmp_path):
    result = NO_DIAGNOSTICS.emit(
        "anything",
        lambda path: (tmp_path / "unexpected.txt").write_text(str(path)),
    )

    assert result is None
    assert not (tmp_path / "unexpected.txt").exists()


def test_child_diagnostics_use_qualified_filters_and_nested_paths(tmp_path):
    diagnostics = Diagnostics(
        enabled=True,
        outdir=tmp_path / "debug",
        only={"stage.input"},
    )
    child = diagnostics.child("stage")

    child.emit("skip", lambda path: (path / "skip.txt").write_text("no"))
    result = child.emit("input", lambda path: (path / "input.txt").write_text("yes"))

    assert result == 3
    assert not (tmp_path / "debug" / "stage" / "skip").exists()
    assert (tmp_path / "debug" / "stage" / "input" / "input.txt").read_text() == "yes"
