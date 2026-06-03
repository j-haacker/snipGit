# snippets

`snippets` is a personal Python utility package. The documentation focuses on
the stable helpers that are useful to reuse. Provenance and reproduction helpers
now live in the standalone `reprotrail` package.

```{toctree}
:maxdepth: 2

provenance
debugging
glossary
api
```

## Install for development

Install the package with the documentation dependencies when working on this
site:

```bash
python -m pip install -e ".[docs]"
```

Build the HTML documentation locally with warnings treated as errors:

```bash
sphinx-build -W -b html docs docs/_build/html
```
