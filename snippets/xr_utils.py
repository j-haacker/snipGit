__all__ = [
    "build_enc_dict__cmpr_f4",
    "get_chunk_number",
    "sel_chunks_by_coord_range",
    "sel_chunks_by_number_range",
]

from collections.abc import Iterable
import numpy as np
import xarray as xr


def build_enc_dict__cmpr_f4(ds: xr.Dataset) -> dict:
    return {
        _var: {"zlib": True, "complevel": 3, "dtype": "float32"}
        for _var in ds.data_vars
        if ds[_var].ndim > 1
    }


def get_chunk_number(ds, dim, coords):
    def _inner(coord):
        if coord < ds[dim][0] or coord > ds[dim][-1]:
            return None
        return (ds[dim].isel({dim: np.cumsum(ds.chunks[ds.dims.index(dim)]) - 1}) <= coord).argmin().item(0)
    if isinstance(coords, Iterable):
        return [_inner(coord) for coord in coords]
    return _inner(coords)


def sel_chunks_by_number_range(ds, dim, start, stop):
    chunk_borders = np.cumsum([0] + list(ds.chunks[ds.dims.index(dim)]))
    return ds.isel({dim: slice(*chunk_borders[[start, stop + 1]])})


def sel_chunks_by_coord_range(ds, **dim_intervals):
    for dim, interval in dim_intervals.items():
        ds = sel_chunks_by_number_range(ds, dim, *get_chunk_number(ds, dim, interval))
    return ds
