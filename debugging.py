__all__ = ["warn_with_traceback"]

import sys
import traceback
import warnings


# CREDIT: mgab https://stackoverflow.com/a/22376126
def warn_with_traceback(message, category, filename, lineno, file=None, line=None):
    log = file if hasattr(file,'write') else sys.stderr
    traceback.print_stack(file=log)
    log.write(warnings.formatwarning(message, category, filename, lineno, line))
    