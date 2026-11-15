"""Small file helpers for proxy configuration."""
import os
from pathlib import Path

def atomic_write(path, text):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        os.chmod(temporary, 0o600)
        handle.write(text)
    os.replace(temporary, path)
