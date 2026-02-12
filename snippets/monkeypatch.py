__all__ = ["gatekeeper", "monkeypatch"]

from contextlib import contextmanager
from packaging.version import Version
import warnings


def gatekeeper(module_version: str, rules: list[dict]):
    """Checks whether a patch should be applied

    Use with a list of dict like

    [{  "version":      "2.3",
        "comparator":   operator.lt,
        "action":       "skip"},
     {  "version":      "3",
        "comparator":   operator.ge,
        "action":       "warn" }]

    Args:
        module_version (str): current version of the patched module
        rules (dict): Requires keys "comparator", "version", and
            "action".

    Returns:
        str: rules["action"] if condition is met, else None
    """
    for rule in rules:
        comparator = rule.get("comparator", rule.get("comperator"))
        if comparator is None:
            raise KeyError("Missing rule key 'comparator'.")
        if comparator(Version(module_version), Version(rule["version"])):
            return rule["action"]


@contextmanager
def monkeypatch(dictlist: list[dict]):
    """Constructs a patched context

    Patching the backend of foreign functions quickly leads to
    inconsistencies. Using the patch only within a chosen context limits
    side effects.

    Optionally, have :func:`gatekeeper` manage for which version
    to apply the patch, to warn about compatibility issues, or to raise
    an error.

    Use like:

    patchdicts = [{ "module":       mod1,
                    "target":       "obj1",
                    "replacement":  patch1,
                    "version":      base_mod1.__version__,  # optional
                    "rules":        rules1},  # optional
                  { "module":       mod2,
                    "target":       "obj2",
                    "replacement":  patch2 }]
    with monkeypatch(patchdicts):
        <your code>

    Args:
        dictlist (list[dict]): Requires keys "module", "target", and
            "replacement".
    """
    for d in dictlist:
        if "rules" in d:
            verdict = gatekeeper(d["version"], d["rules"])
            if verdict == "skip":
                continue
            elif verdict == "raise":
                raise
            elif verdict == "warn":
                warnings.warn(
                    f"Patch not meant for {d['module']} version {d['version']}."
                )
        d.update({"original": getattr(d["module"], d["target"])})
        setattr(d["module"], d["target"], d["replacement"])
    try:
        yield
    finally:
        for d in dictlist:
            if "original" in d:
                setattr(d["module"], d["target"], d["original"])
