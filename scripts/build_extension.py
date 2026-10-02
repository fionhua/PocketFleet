#!/usr/bin/env python3
"""Build and packaging script for PocketFleet Browser Extension.

Validates manifest.json, checks integrity of content scripts,
synchronizes assets to assets/browser-extension/, and produces
dist/pocketfleet-browser-extension.zip for production distribution.
"""
from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "browser-extension"
ASSETS_DIR = REPO_ROOT / "assets" / "browser-extension"
DIST_DIR = REPO_ROOT / "dist"
DIST_ZIP = DIST_DIR / "pocketfleet-browser-extension.zip"


def validate_manifest(src: Path) -> dict:
    manifest_file = src / "manifest.json"
    if not manifest_file.is_file():
        raise FileNotFoundError(f"Missing manifest.json at {manifest_file}")
    with open(manifest_file, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    required_keys = ["manifest_version", "name", "version"]
    for k in required_keys:
        if k not in manifest:
            raise ValueError(f"manifest.json missing required key: {k}")

    if manifest.get("manifest_version") != 3:
        raise ValueError(f"Expected manifest_version 3, got: {manifest.get('manifest_version')}")

    print(f"✅ Validated manifest.json: {manifest['name']} v{manifest['version']}")
    return manifest


def build_extension() -> int:
    print(f"🧩 Building PocketFleet Browser Extension from: {SRC_DIR}")
    if not SRC_DIR.is_dir():
        print(f"❌ Error: Source directory does not exist: {SRC_DIR}", file=sys.stderr)
        return 1

    try:
        manifest = validate_manifest(SRC_DIR)
    except Exception as e:
        print(f"❌ Manifest validation failed: {e}", file=sys.stderr)
        return 1

    # Check required core files
    core_files = [
        "background.js",
        "content.js",
        "content.css",
        "popup.html",
        "popup.js",
        "adapter_profiles.js",
    ]
    for fn in core_files:
        p = SRC_DIR / fn
        if not p.is_file():
            print(f"❌ Missing core file: {fn}", file=sys.stderr)
            return 1
        print(f"  • Verified {fn} ({p.stat().st_size} bytes)")

    # 1. Sync to assets/browser-extension
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    for item in SRC_DIR.iterdir():
        dest = ASSETS_DIR / item.name
        if item.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)
    print(f"✅ Synchronized extension to assets directory: {ASSETS_DIR}")

    # 2. Package into dist/pocketfleet-browser-extension.zip
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    if DIST_ZIP.exists():
        DIST_ZIP.unlink()

    with zipfile.ZipFile(DIST_ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in SRC_DIR.rglob("*"):
            if file_path.is_file():
                rel_path = file_path.relative_to(SRC_DIR)
                zf.write(file_path, arcname=str(rel_path))

    zip_size = DIST_ZIP.stat().st_size
    print(f"✅ Compiled distribution archive: {DIST_ZIP} ({zip_size} bytes)")
    print(f"🎉 Build completed successfully: {manifest['name']} v{manifest['version']}")
    return 0


if __name__ == "__main__":
    sys.exit(build_extension())
