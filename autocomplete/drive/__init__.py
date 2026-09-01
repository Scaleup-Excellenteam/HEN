"""Optional import of text documents from a user's Google Drive.

This package is an extension and is switched off unless it is configured. With
no configuration nothing here is imported by the command line, nothing contacts
Google, and search behaves exactly as it does without the package present.

The pieces, in the order a document travels through them:

``settings``  what the feature is allowed to do, read from the environment
``client``    the only code that speaks to Google
``documents`` turning a Drive file into validated, decoded corpus text
``store``     the imported corpus on disk and its atomically published index
``jobs``      the one-at-a-time import and removal lifecycle

Ranking across the corpus and the imported documents is not done here: it is
:mod:`autocomplete.composite`, which knows nothing about Drive.
"""

from .errors import DriveError
from .settings import DriveSettings, load_settings

__all__ = ["DriveError", "DriveSettings", "load_settings"]
