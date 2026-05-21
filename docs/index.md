# snippets

`snippets` is a personal Python utility package. The documentation focuses on
the stable helpers that are useful to reuse, especially the provenance tools for
recording software state, input state, and CF/xarray history metadata.

```{toctree}
:maxdepth: 2

provenance
glossary
api
```

## Install for development

Install the package with the documentation dependencies when working on this
site:

```bash
python -m pip install -e ".[docs,full]"
```

Build the HTML documentation locally with warnings treated as errors:

```bash
sphinx-build -W -b html docs docs/_build/html
```
