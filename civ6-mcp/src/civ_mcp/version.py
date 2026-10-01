"""Version and git metadata — computed once at import time."""

import importlib.metadata
import subprocess
from pathlib import Path


def _get_version() -> str:
    # Accept both distribution spellings ("civ-mcp" legacy, "civ6-mcp" current).
    for dist in ("civ-mcp", "civ6-mcp"):
        try:
            return importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            pass
    # Fallback: read from pyproject.toml (uv run doesn't install metadata).
    # NOTE: explicit encoding — the default is GBK on zh-CN Windows and the
    # description line contains a non-ASCII dash (crash reproduced 2026-09-30).
    pyproject = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
    if pyproject.exists():
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("version =") or line.strip().startswith(
                "version="
            ):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "0.0.0-dev"


def _get_git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return ""


def _get_git_describe() -> str:
    """Full version descriptor: tag + commits-since-tag + SHA + dirty flag.

    Example: ``v1.0.4-3-g2630adb-dirty``
    Falls back to short SHA if no tags exist, or empty string.
    """
    try:
        return subprocess.check_output(
            ["git", "describe", "--tags", "--always", "--dirty"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return ""


VERSION = _get_version()
GIT_SHA = _get_git_sha()
GIT_DESCRIBE = _get_git_describe()
