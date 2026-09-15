from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


def create_directory_link(link: Path, target: Path) -> None:
    """Create a real directory link, using a Windows junction without privileges."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as symlink_error:
        if os.name != "nt":
            pytest.skip(f"directory symlink creation is unavailable: {symlink_error}")
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", os.fspath(link), os.fspath(target)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.skip(
            "directory symlink and junction creation are unavailable: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
