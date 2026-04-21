#!/usr/bin/env python3
"""
Build a hotfix zip for the Document Retrieval System.
Files are stored WITHOUT a wrapper folder so the zip can be
extracted directly into C:\DOC_RETRIEVAL_SYSTEM.

Usage:
    python build_hotfix.py                          # default file list
    python build_hotfix.py file1.py file2.jsx ...   # custom file list
"""
import os
import sys
import zipfile
from pathlib import Path

# Fix Windows console encoding
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PROJ_ROOT = Path(__file__).parent.resolve()
VERSION = (PROJ_ROOT / "VERSION").read_text().strip()
ZIP_NAME = f"HOTFIX_v{VERSION}.zip"
OUT_ZIP = PROJ_ROOT / ZIP_NAME

# Default hotfix files — update as needed
DEFAULT_FILES = [
    "VERSION",
    "backend/app/agents/pipeline.py",
    "backend/app/api/approvals.py",
    "backend/app/api/documents.py",
    "backend/app/api/oauth_microsoft.py",
    "backend/app/core/security.py",
    "backend/app/main.py",
    "backend/app/services/imap_service.py",
    "frontend/src/main.jsx",
    "scripts/install_services.ps1",
]


def build(file_list: list[str]):
    print()
    print("=" * 60)
    print(f"  Building HOTFIX v{VERSION}")
    print("=" * 60)
    print()

    os.chdir(PROJ_ROOT)

    included = []
    missing = []
    for f in file_list:
        if os.path.exists(f):
            included.append(f)
        else:
            missing.append(f)

    if missing:
        print("  WARNING — missing files (skipped):")
        for f in missing:
            print(f"    {f}")
        print()

    if not included:
        print("  ERROR: no files to include!")
        sys.exit(1)

    if OUT_ZIP.exists():
        OUT_ZIP.unlink()

    with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in sorted(included):
            zf.write(f, f)  # NO wrapper folder
            print(f"  + {f}")

    size_kb = OUT_ZIP.stat().st_size / 1024
    print()
    print("=" * 60)
    print(f"  Done!  {ZIP_NAME}  ({size_kb:.0f} KB, {len(included)} files)")
    print("=" * 60)
    print()
    print("  Install:")
    print(f"    Extract into C:\\DOC_RETRIEVAL_SYSTEM (no wrapper folder)")
    print("    Then: Restart-Service DRS-Backend")
    print()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        build(sys.argv[1:])
    else:
        build(DEFAULT_FILES)
