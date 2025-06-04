# mypackage/__init__.py
from . import module1
from . import module2

# หรือจาก mypackage import *
__all__ = ["module1", "module2", "some_function"]