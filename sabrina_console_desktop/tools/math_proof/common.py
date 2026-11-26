import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def load(path):
    return json.loads(Path(path).read_bytes())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


@contextmanager
def compute(client):
    """A rejected, synchronously finished experiment releases its reservation."""
    error = None
    with client.lease("compute:lab", wait_seconds=1200):
        try:
            yield
        except (ValueError, SyntaxError, TypeError, KeyError, RuntimeError) as exc:
            error = exc
    if error is not None:
        raise error
