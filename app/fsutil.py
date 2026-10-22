"""Small file-system helpers."""

import os
import time
from pathlib import Path

REPLACE_ATTEMPTS = 8
REPLACE_WAIT = 0.05


def replace_file(src: Path, dst: Path) -> None:
    """`os.replace`, retried briefly. On Windows a virus scanner or indexer can hold a file that was
    just written for a moment, which fails the rename with PermissionError."""
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_WAIT * (attempt + 1))
