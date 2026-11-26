"""Build Sabrina's browser image on the Docker engine used by the desktop."""

import io
import subprocess
import tarfile
from pathlib import Path

from . import IMAGE, docker_command


def main() -> None:
    folder = Path(__file__).parent
    context = io.BytesIO()
    with tarfile.open(fileobj=context, mode="w:gz") as archive:
        for name in ("Dockerfile", "__init__.py", "protocol.py", "worker.py"):
            archive.add(folder / name, arcname=name)
    subprocess.run(docker_command("build", "-t", IMAGE, "-"), input=context.getvalue(), check=True)


if __name__ == "__main__":
    main()
