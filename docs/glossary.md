# Glossary

The provenance terms below are kept for readers of older `snippets` workflows.
The active provenance and reproduction implementation is now `reprotrail`.

Backend
: The storage or tracking system responsible for an input path. Supported
  values are `dvc`, `git-lfs`, `git`, `filesystem`, and `unknown`.

CF history
: A newline-separated metadata field used by CF-style datasets. New entries are
  prepended so the latest processing step appears first.

Clean repository
: A Git repository whose short status is empty.

Dirty repository
: A Git repository with staged, unstaged, or untracked changes. Dirty states are
  marked with `+dirty`.

Diff hash
: A SHA-256 hash of the dirty status plus staged and unstaged diff text. It
  distinguishes dirty states without publishing the full local patch.

Input path state
: A record describing whether an input exists, what kind of path it is, which
  backend owns it, and which public metadata can identify it later.

Provenance metadata
: Metadata that records where an output came from: software repository state,
  command history, and input path state.

Public provenance
: A compact provenance record intended to be embedded in outputs. It omits
  local-only paths such as repository roots.
