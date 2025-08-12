"""Python snippets"""

__version__ = "0.1.0"

__all__ = [
    "is_notebook",
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


def is_notebook() -> bool:
    # CREDIT Gustavo Bezerra https://stackoverflow.com/a/39662359
    try:
        shell = get_ipython().__class__.__name__  # type: ignore
        if shell == 'ZMQInteractiveShell':
            return True   # Jupyter notebook or qtconsole
        elif shell == 'TerminalInteractiveShell':
            return False  # Terminal running IPython
        else:
            print(f"`get_ipython().__class__.__name__` {shell} is not"
                  "considered notebook.")
            return False  # Other type (?)
    except NameError:
        return False      # Probably standard Python interpreter


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
