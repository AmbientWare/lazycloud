"""Build the Next.js static export into the web image; the image build runs this.

python web/build.py /opt/image-studio/web
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

NODE_VERSION = "24.21.0"
NODE_ARCHIVE = f"node-v{NODE_VERSION}-linux-x64.tar.xz"
NODE_SHA256 = "fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6"
WEB_DIR = Path(__file__).resolve().parent


def install_node(scratch: Path) -> Path:
    archive = scratch / NODE_ARCHIVE
    urllib.request.urlretrieve(f"https://nodejs.org/dist/v{NODE_VERSION}/{NODE_ARCHIVE}", archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != NODE_SHA256:
        raise SystemExit(f"{NODE_ARCHIVE} has sha256 {digest}, expected {NODE_SHA256}")
    with tarfile.open(archive) as tar:
        tar.extractall(scratch, filter="data")
    return scratch / NODE_ARCHIVE.removesuffix(".tar.xz") / "bin"


def main(output: Path) -> None:
    with TemporaryDirectory() as scratch:
        node_bin = install_node(Path(scratch))
        env = {
            **os.environ,
            "PATH": f"{node_bin}{os.pathsep}{os.environ['PATH']}",
            "NEXT_TELEMETRY_DISABLED": "1",
        }
        subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=WEB_DIR, env=env, check=True)
        subprocess.run(["npm", "run", "build"], cwd=WEB_DIR, env=env, check=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(WEB_DIR / "out", output)
    # Removed in the same build step, so the image layer never holds them.
    shutil.rmtree(WEB_DIR / "node_modules")
    shutil.rmtree(WEB_DIR / ".next")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
