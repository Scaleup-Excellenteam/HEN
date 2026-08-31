"""An optional HTTP interface over the completion engine.

The command line in ``main.py`` remains the interface the assignment asks for.
This exists so a browser can reach the same engine, and it adds nothing to the
search: it prepares the index once, calls the public function, and returns what
comes back in the order it comes back.
"""

from .api import create_app

__all__ = ["create_app"]
