"""Utilities for inspecting and slicing xarray objects."""

__all__ = [
    "build_enc_dict__cmpr_f4",
    "get_chunk_number",
    "print_xarray_dataset_summary",
    "sel_chunks_by_coord_range",
    "sel_chunks_by_number_range",
    "xarray_dataset_summary",
]

from collections.abc import Iterable
import numpy as np
import sys
from typing import Any, Hashable, TextIO
import xarray as xr


def _as_dataset(obj: xr.Dataset | xr.DataArray) -> xr.Dataset:
    if isinstance(obj, xr.Dataset):
        return obj
    if isinstance(obj, xr.DataArray):
        name = obj.name if obj.name is not None else "data"
        return obj.to_dataset(name=name)
    raise TypeError("obj must be an xarray Dataset or DataArray.")


def _xarray_variable_summary(
    name: str,
    variable: xr.Variable,
    *,
    prefix: str,
) -> tuple[str, str]:
    data = variable.data
    backend = type(data).__name__
    chunks = getattr(variable, "chunks", None) or getattr(data, "chunks", None)
    is_lazy = callable(getattr(data, "__dask_graph__", None)) or chunks is not None
    if is_lazy:
        status = "lazy"
    elif isinstance(data, np.ndarray):
        status = "loaded"
    else:
        status = "other"
    chunk_text = f", chunks={chunks!r}" if chunks is not None else ""
    line = (
        f"{prefix} variable {name!r}: status={status}, dims={variable.dims!r}, "
        f"shape={variable.shape!r}, dtype={variable.dtype}, backend={backend}"
        f"{chunk_text}, nbytes={variable.nbytes}"
    )
    return status, line


def xarray_dataset_summary(
    obj: xr.Dataset | xr.DataArray,
    *,
    label: str | None = None,
    prefix: str = "[xarray-debug]",
) -> str:
    """Summarize xarray structure and lazy/eager state without computing data."""

    ds = _as_dataset(obj)
    lines = [f"{prefix} {label}" if label is not None else prefix, str(ds)]
    summaries = [
        _xarray_variable_summary(name, variable, prefix=prefix)
        for name, variable in ds.variables.items()
    ]
    lazy = sum(status == "lazy" for status, _ in summaries)
    loaded = sum(status == "loaded" for status, _ in summaries)
    other = len(summaries) - lazy - loaded
    lines.append(
        f"{prefix} variables: total={len(summaries)}, "
        f"lazy={lazy}, loaded={loaded}, other={other}"
    )
    lines.extend(line for _, line in summaries)
    return "\n".join(lines)


def print_xarray_dataset_summary(
    obj: xr.Dataset | xr.DataArray,
    *,
    label: str | None = None,
    prefix: str = "[xarray-debug]",
    file: TextIO | None = None,
) -> None:
    """Print :func:`xarray_dataset_summary` to a file-like object."""

    target = sys.stdout if file is None else file
    print(xarray_dataset_summary(obj, label=label, prefix=prefix), file=target)


def build_enc_dict__cmpr_f4(ds: xr.Dataset) -> dict:
    """Build NetCDF compression encoding for multi-dimensional data variables."""

    return {
        _var: {"zlib": True, "complevel": 3, "dtype": "float32"}
        for _var in ds.data_vars
        if ds[_var].ndim > 1
    }


def get_chunk_number(da: xr.DataArray, dim: Hashable, coords: Any) -> list[Any]:
    """Return chunk indices containing one or more coordinates along a dimension."""

    def _inner(coord):
        if coord < da[dim][0] or coord > da[dim][-1]:
            return None
        return (
            (da[dim].isel({dim: np.cumsum(da.chunks[da.dims.index(dim)]) - 1}) <= coord)
            .argmin()
            .item(0)
        )

    if isinstance(coords, Iterable):
        return [_inner(coord) for coord in coords]
    return [_inner(coords)]


def sel_chunks_by_number_range(da: xr.DataArray, dim: Hashable, start: int, stop: int):
    """Select a contiguous inclusive range of chunk numbers along a dimension."""

    chunk_borders = np.cumsum([0] + list(da.chunks[da.dims.index(dim)]))
    return da.isel({dim: slice(*chunk_borders[[start, stop + 1]])})


def sel_chunks_by_coord_range(da: xr.DataArray, **dim_intervals) -> xr.DataArray:
    """Select chunk ranges that contain coordinate intervals for each dimension."""

    for dim, interval in dim_intervals.items():
        da = sel_chunks_by_number_range(da, dim, *get_chunk_number(da, dim, interval))
    return da
