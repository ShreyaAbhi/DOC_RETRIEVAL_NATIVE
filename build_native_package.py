#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Document Retrieval System -- Native Package Builder
Run from the project root:  python build_native_package.py
Produces:  DOC_RETRIEVAL_SYSTEM_v1_native.zip
"""
import os
import sys
import shutil
import zipfile
import tempfile
from pathlib import Path

# Fix Windows console encoding
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

VERSION   = (Path(__file__).parent / "VERSION").read_text().strip()
ZIP_NAME  = f"DOC_RETRIEVAL_SYSTEM_v{VERSION}_native.zip"
PROJ_ROOT = Path(__file__).parent.resolve()
OUT_ZIP   = PROJ_ROOT / ZIP_NAME

# ── Files / dirs that must NEVER be in the package ────────────────────────
ALWAYS_EXCLUDE = {
    "__pycache__", ".pyc", ".pyo",
    "node_modules", ".vite", "dist",
    ".env",                           # live secrets
    ".git", ".gitignore",
    "celerybeat-schedule",            # runtime artefacts
    "celerybeat-schedule-shm",
    "celerybeat-schedule-wal",
    ".claude",
    "build_native_package.py",
    "CLAUDE_CODE_INSTRUCTIONS.md",
    "test_api.sh",
    "test_pipeline_security.sh",
    "gen_files.py",
    "tests",                          # unit-test directory
    "venv",                           # virtualenv — recipient creates their own
    "pod_system.db",                  # SQLite database — recipient gets a fresh one
    "storage",                        # runtime data
    "pod_storage",
    "packing_slips",
    "invoices",
    "order_import",
    "documents",
    # Docker artefacts — native install needs no Docker
    "Dockerfile",
    "Dockerfile.prod",
    "docker-compose.yml",
    "docker-compose.yaml",
    ".dockerignore",
    "nginx.spa.conf",                 # nginx config is Docker-only
    # Sensitive / signing keys
    "private.pem",
}

# File suffixes that must never be in the package
EXCLUDE_SUFFIXES = {
    ".pyc", ".pyo",
    ".db",                            # SQLite databases (all of them)
    ".db-shm",                        # SQLite WAL shared-memory file
    ".db-wal",                        # SQLite WAL log
}

def should_exclude(path: Path) -> bool:
    for part in path.parts:
        if part in ALWAYS_EXCLUDE:
            return True
    if path.suffix in EXCLUDE_SUFFIXES:
        return True
    # Also catch multi-part suffixes like .db-shm / .db-wal via name check
    name_lower = path.name.lower()
    if name_lower.endswith(".db-shm") or name_lower.endswith(".db-wal"):
        return True
    if path.name.startswith("."):
        return True
    return False


def copy_tree(src: Path, dst: Path):
    """Recursively copy src → dst, honouring exclusion rules."""
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        rel = item.relative_to(src)
        if should_exclude(rel):
            continue
        target = dst / item.name
        if item.is_dir():
            copy_tree(item, target)
        else:
            shutil.copy2(item, target)


def build():
    print()
    print("=" * 60)
    print("  Document Retrieval System -- Building Native Package")
    print("=" * 60)
    print()

    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "DOC_RETRIEVAL_SYSTEM"
        pkg.mkdir()

        # ── 1. Backend (no venv, pycache, runtime data) ────────────
        print("[1/6] Copying backend...")
        copy_tree(PROJ_ROOT / "backend", pkg / "backend")

        # ── 2. Frontend (no node_modules, dist, .vite) ─────────────
        print("[2/6] Copying frontend...")
        copy_tree(PROJ_ROOT / "frontend", pkg / "frontend")

        # ── 3. Scripts ──────────────────────────────────────────────
        print("[3/6] Copying scripts...")
        copy_tree(PROJ_ROOT / "scripts", pkg / "scripts")

        # ── 4. Installer and launcher files ─────────────────────────
        print("[4/5] Copying installer and launcher files...")
        shutil.copy2(PROJ_ROOT / "installer.ps1", pkg / "installer.ps1")
        shutil.copy2(PROJ_ROOT / "Install.bat",   pkg / "Install.bat")
        shutil.copy2(PROJ_ROOT / "LAUNCH.ps1",    pkg / "LAUNCH.ps1")
        shutil.copy2(PROJ_ROOT / "LAUNCH.bat",    pkg / "LAUNCH.bat")
        shutil.copy2(PROJ_ROOT / "update.ps1",    pkg / "update.ps1")
        shutil.copy2(PROJ_ROOT / "UPDATE.bat",    pkg / "UPDATE.bat")
        shutil.copy2(PROJ_ROOT / "VERSION",       pkg / "VERSION")

        # ── 5. Empty storage directories (created at runtime) ───────
        print("[5/5] Creating empty storage directories...")
        storage_root = pkg / "backend" / "storage"
        for d in ("pod_storage", "packing_slips", "invoices", "documents", "order_import"):
            dir_path = storage_root / d
            dir_path.mkdir(parents=True, exist_ok=True)
            (dir_path / ".gitkeep").touch()

        # ── Zip it up ───────────────────────────────────────────────
        print()
        print(f"  Compressing → {ZIP_NAME} ...")
        if OUT_ZIP.exists():
            OUT_ZIP.unlink()

        with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for f in sorted(pkg.rglob("*")):
                if f.is_file():
                    arcname = f.relative_to(tmp)   # DOC_RETRIEVAL_SYSTEM/...
                    zf.write(f, arcname)

    size_mb = OUT_ZIP.stat().st_size / (1024 * 1024)
    print()
    print("=" * 60)
    print(f"  Done!  {ZIP_NAME}  ({size_mb:.1f} MB)")
    print("=" * 60)
    print()
    print("  Package contents:")
    print("    backend/              — Application code (FastAPI)")
    print("    backend/storage/      — Empty storage dirs (filled at runtime)")
    print("    frontend/             — Application code (React)")
    print("    scripts/              — Start/stop scripts")
    print("    installer.ps1         — Setup wizard (WinForms GUI)")
    print("    Install.bat           — Double-click launcher")
    print("    update.ps1            — Auto-updater (checks GitHub Releases)")
    print("    UPDATE.bat            — Double-click updater")
    print(f"    VERSION               — {VERSION}")
    print("    (SQLite DB created automatically on first launch — no init.sql needed)")
    print()
    print("  To install on a target machine:")
    print("    1. Extract the zip to any folder")
    print("    2. Double-click  Install.bat")
    print()
    print(f"  Package location:")
    print(f"    {OUT_ZIP}")
    print()


if __name__ == "__main__":
    build()
