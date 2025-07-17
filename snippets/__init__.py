"""Python snippets"""

__version__ = "0.1.0"

__all__ = [
    "wrap_sys_exit",
    # modules
    "debugging",
    "monkeypatch",
    "unsupervised",
    "parallel",
    "xr_utils",
]

from contextlib import contextmanager
from sys import exit


@contextmanager
def wrap_sys_exit(code_dict: set[tuple[Exception,int]] = None):
    try:
        yield
    except Exception as err:
        if code_dict is None:
            exit(1)
        else:
            for _exception, exit_code in code_dict:
                if isinstance(err, _exception):
                    exit(exit_code)
            exit(1)
    else:
        exit(0)
