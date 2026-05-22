"""Debugging helpers for interactive analysis and small workflow scripts."""

__all__ = ["Diagnostics", "NO_DIAGNOSTICS", "NoDiagnostics", "warn_with_traceback"]

from collections.abc import Callable, Iterable
from pathlib import Path
import sys
import traceback
from typing import Any
import warnings


class NoDiagnostics:
    """Disabled diagnostic artifact hooks for default/off state."""

    enabled = False
    outdir = Path("debug")
    only = None

    def active(self, name: str) -> bool:
        return False

    def emit(self, name: str, fn: Callable[[Path], Any]) -> Any:
        return None

    def child(self, prefix: str) -> "NoDiagnostics":
        return self


NO_DIAGNOSTICS = NoDiagnostics()


class Diagnostics:
    """Small opt-in writer for diagnostic artifacts.

    Use this in external scripts or home-brewed workflow code when plots,
    reports, or snapshots are useful for debugging an operation.
    """

    def __init__(
        self,
        enabled: bool = False,
        outdir: Path | str = "debug",
        only: Iterable[str] | None = None,
        *,
        _prefix: str | None = None,
    ):
        self.enabled = enabled
        self.outdir = Path(outdir)
        self.only = set(only) if only is not None else None
        self._prefix = _prefix

    def _qualify(self, name: str) -> str:
        if self._prefix is None or self._prefix == "":
            return name
        return f"{self._prefix}.{name}"

    def active(self, name: str) -> bool:
        qualified = self._qualify(name)
        return self.enabled and (self.only is None or qualified in self.only)

    def emit(self, name: str, fn: Callable[[Path], Any]) -> Any:
        if not self.active(name):
            return None
        path = self.outdir.joinpath(*self._qualify(name).split("."))
        path.mkdir(parents=True, exist_ok=True)
        return fn(path)

    def child(self, prefix: str) -> "Diagnostics":
        return Diagnostics(
            enabled=self.enabled,
            outdir=self.outdir,
            only=self.only,
            _prefix=self._qualify(prefix),
        )


# CREDIT: mgab https://stackoverflow.com/a/22376126
def warn_with_traceback(message, category, filename, lineno, file=None, line=None):
    log = file if hasattr(file, "write") else sys.stderr
    traceback.print_stack(file=log)
    log.write(warnings.formatwarning(message, category, filename, lineno, line))
