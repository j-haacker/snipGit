# Provenance And Reproduction

The provenance and reproduction helpers have moved out of `snippets` into the
standalone `reprotrail` package.

Use `reprotrail.provenance` for Git state, input path state, public provenance,
and CF/xarray history helpers:

```python
from reprotrail.provenance import build_cf_history_entry, get_git_state
```

Use `reprotrail reproduce` or `reprotrail.reproduce` to create reproduction
workspaces from product provenance sidecars:

```bash
reprotrail reproduce --provenance results/product.prov.json --workspace /tmp/repro
```

The `reprotrail` source tree is available next to this repository at
`../reprotrail`.
