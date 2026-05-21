from io import StringIO

import numpy as np
import pytest
import xarray as xr

from snippets.xr_utils import print_xarray_dataset_summary, xarray_dataset_summary


def test_xarray_dataset_summary_reports_loaded_variables():
    ds = xr.Dataset({"tas": xr.DataArray(np.array([1.0, 2.0]), dims="time")})

    out = xarray_dataset_summary(ds, label="eager dataset")

    assert "[xarray-debug] eager dataset" in out
    assert "Dimensions:" in out
    assert "lazy=0" in out
    assert "loaded=1" in out
    assert "variable 'tas': status=loaded" in out
    assert "backend=ndarray" in out


def test_xarray_dataset_summary_reports_lazy_variables():
    pytest.importorskip("dask.array")
    ds = xr.Dataset(
        {"tas": xr.DataArray(np.arange(4.0), dims="time").chunk({"time": 2})}
    )

    out = xarray_dataset_summary(ds, label="lazy dataset")

    assert "[xarray-debug] lazy dataset" in out
    assert "lazy=1" in out
    assert "loaded=0" in out
    assert "variable 'tas': status=lazy" in out
    assert "chunks=" in out


def test_xarray_dataset_summary_accepts_dataarray():
    da = xr.DataArray(np.array([1.0]), dims="time", name="tas")

    out = xarray_dataset_summary(da)

    assert "variable 'tas': status=loaded" in out


def test_print_xarray_dataset_summary_writes_to_file():
    ds = xr.Dataset({"tas": xr.DataArray(np.array([1.0]), dims="time")})
    file = StringIO()

    print_xarray_dataset_summary(ds, label="capture", file=file)

    assert "[xarray-debug] capture" in file.getvalue()
