"""Optional original experiment replay; private evidence is never bundled here."""
from pathlib import Path
import os
import unittest


def private_folder(name: str) -> Path:
    root = os.environ.get("BROTHER_PRIVATE_REPLAY_DIR")
    if root is None:
        raise unittest.SkipTest("Original private experiment proof not distributed; synthetic regressions run separately")
    path = Path(root) / name
    if not path.is_dir():
        raise AssertionError("Explicit private replay directory is missing " + name)
    return path
