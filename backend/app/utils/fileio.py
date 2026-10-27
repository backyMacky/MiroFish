"""Publish complete JSON files for concurrent status readers."""

import json
import os
import tempfile
from pathlib import Path


def atomic_write_json(path, value):
    target = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f'.{target.name}.', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
