"""PocketFleet Distribution Builder

Builds standard wheel & sdist packages for PyPI / release distribution.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def build() -> int:
    print("=" * 60)
    print(" [*] PocketFleet Packaging Pipeline")
    print("=" * 60)

    # Clean old dist
    dist_dir = REPO_ROOT / "dist"
    if dist_dir.exists():
        import shutil
        shutil.rmtree(dist_dir)
        print("  [-] Cleaned old dist/ directory")

    # Run build
    cmd = [sys.executable, "-m", "pip", "install", "build"]
    subprocess.run(cmd, check=True)

    cmd_build = [sys.executable, "-m", "build", str(REPO_ROOT)]
    res = subprocess.run(cmd_build, cwd=str(REPO_ROOT))
    if res.returncode != 0:
        print("  [x] Build failed!", file=sys.stderr)
        return res.returncode

    print("\n  [+] Package built successfully:")
    for f in (REPO_ROOT / "dist").glob("*"):
        size_kb = f.stat().st_size / 1024
        print(f"     - {f.name} ({size_kb:.1f} KB)")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(build())
