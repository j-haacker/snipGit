__all__ = [
    "build_enc_dict__cmpr_f4",
    "get_chunk_number",
    "sel_chunks_by_coord_range",
    "sel_chunks_by_number_range",
]

from collections.abc import Iterable
import numpy as np
from typing import Any, Hashable
import xarray as xr


def build_enc_dict__cmpr_f4(ds: xr.Dataset) -> dict:
    return {
        _var: {"zlib": True, "complevel": 3, "dtype": "float32"}
        for _var in ds.data_vars
        if ds[_var].ndim > 1
    }


def get_chunk_number(da: xr.DataArray, dim: Hashable, coords: Any) -> list[Any]:
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
    chunk_borders = np.cumsum([0] + list(da.chunks[da.dims.index(dim)]))
    return da.isel({dim: slice(*chunk_borders[[start, stop + 1]])})


def sel_chunks_by_coord_range(da: xr.DataArray, **dim_intervals) -> xr.DataArray:
    for dim, interval in dim_intervals.items():
        da = sel_chunks_by_number_range(da, dim, *get_chunk_number(da, dim, interval))
    return da
