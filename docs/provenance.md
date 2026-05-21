# Provenance

The provenance module records enough context to make generated outputs easier to
audit later. It captures Git repository state, classifies input paths, and
formats compact history entries that can be stored in CF-compliant metadata or
in an xarray object's `attrs["history"]` value.

## Repository state

Use `get_git_state()` to capture the current commit, branch, canonical remote
URL, and dirty working-tree status. Dirty repositories are marked with
`+dirty`; when enabled, a hash of the staged and unstaged diff payload is added
so two dirty states can be distinguished without embedding the full patch in
public metadata.

Use `enforce_clean_repos()` before writing important outputs when a workflow
should fail unless all required repositories are clean. Set `allow_dirty=True`
only when the caller deliberately accepts non-reproducible local changes.

## Input path state

Use `get_input_path_state()` for files or directories that feed a workflow. The
module classifies each path with one backend:

- `dvc` when DVC metadata is found.
- `git-lfs` when a Git LFS pointer or LFS tracking metadata is found.
- `git` when the path is tracked in Git.
- `filesystem` when the path exists outside the tracked backends.
- `unknown` when the path cannot be resolved to an existing input.

Directories are summarized by file count, total bytes, and a manifest hash based
on relative paths, sizes, and modification times.

## Public provenance

Use `public_provenance()` before writing metadata into public outputs. It
removes local-only details such as repository roots and source paths, while
keeping portable fields such as repository name, commit, branch, canonical
remote URL, dirty marker, input backend, and relevant DVC or Git LFS metadata.

## CF and xarray history

Use `build_cf_history_entry()` to format a timestamped history line. The helper
normalizes selected command paths, strips `--provenance-json` arguments, and can
include compact software and input summaries.

Use `append_cf_history()` or `append_xarray_history()` to prepend the newest
entry to existing history text.

```python
from snippets.provenance import (
    append_xarray_history,
    build_cf_history_entry,
    get_git_state,
)

entry = build_cf_history_entry(
    ["python", "-m", "workflow", "run"],
    git_state=get_git_state("."),
)
dataset = append_xarray_history(dataset, entry, copy=True)
```
