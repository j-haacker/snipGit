__all__ = [
    "build_enc_dict__cmpr_f4",
]

import xarray as xr


def build_enc_dict__cmpr_f4(ds: xr.Dataset) -> dict:
    return {
        _var: {"zlib": True, "complevel": 3, "dtype": "float32"}
        for _var in ds.data_vars
        if ds[_var].ndim > 1
    }
