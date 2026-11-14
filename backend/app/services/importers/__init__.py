"""Format-specific document importers."""

from pathlib import Path
from typing import Any, Callable, Dict, Optional

from app.services.document_model import DocumentModel


class PasswordRequiredError(Exception):
    """Raised when a document is encrypted and requires a password."""


class CorruptFileError(Exception):
    """Raised when a document is corrupt or unreadable."""


ProgressCallback = Optional[Callable[[int, int, str], None]]
