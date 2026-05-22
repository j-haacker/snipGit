# Debugging

The debugging helpers are meant for external scripts and small home-brewed
workflow code. `Diagnostics` provides an opt-in pattern for writing artifacts
such as plots, reports, or intermediate snapshots without adding global debug
flags or commented-out debugging blocks.

When diagnostics are disabled, hooks return immediately and do not create output
directories:

```python
from snippets.debugging import Diagnostics, NO_DIAGNOSTICS


def write_report(value, path):
    path.write_text(f"value={value}\n", encoding="utf-8")


def process_value(value, diagnostics=NO_DIAGNOSTICS):
    diagnostics.emit(
        "input",
        lambda path: write_report(value, path / "input.txt"),
    )

    result = value * 2

    diagnostics.emit(
        "output",
        lambda path: write_report(result, path / "output.txt"),
    )
    return result


diagnostics = Diagnostics(enabled=True, outdir="debug", only={"output"})
process_value(3, diagnostics=diagnostics)
```

Use `child()` to keep event names explicit in nested workflows:

```python
diagnostics = Diagnostics(
    enabled=True,
    outdir="debug",
    only={"preprocess.input"},
)
preprocess_diagnostics = diagnostics.child("preprocess")
preprocess_diagnostics.emit("input", lambda path: write_report(3, path / "input.txt"))
```

This writes to `debug/preprocess/input/input.txt`. Because child diagnostics use
qualified event names, the `only` filter must use `preprocess.input`, not
`input`.

## Optional dependency fallback

If a workflow can run without `snippets`, define a small fallback and skip
optional setup when `Diagnostics` is unavailable:

```python
try:
    from snippets.debugging import NO_DIAGNOSTICS, Diagnostics
except ImportError:
    class NoDiagnostics:
        enabled = False

        def active(self, name):
            return False

        def emit(self, name, fn):
            return None

        def child(self, prefix):
            return self

    NO_DIAGNOSTICS = NoDiagnostics()
    Diagnostics = None


diagnostics = NO_DIAGNOSTICS
if Diagnostics is not None:
    diagnostics = Diagnostics(enabled=True, outdir="debug")
```

Functions can still default to `NO_DIAGNOSTICS`, so callers that do not need
artifacts do not need any setup.

## Performance

Diagnostic hooks belong at stage boundaries. Do not put `emit()` calls inside
tight numerical loops, per-cell loops, Dask block kernels, numba or JAX compiled
sections, or xarray `apply_ufunc` inner functions.

In hot paths, check `diagnostics.active(name)` before doing any setup for a
diagnostic artifact.
