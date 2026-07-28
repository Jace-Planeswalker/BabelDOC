from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _environment(cache_home: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment["BABELDOC_CACHE_HOME"] = cache_home
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{ROOT}{os.pathsep}{existing}" if existing else str(ROOT)
    )
    return environment


def test_absolute_cache_home_controls_import_time_cache(tmp_path: Path) -> None:
    cache = tmp_path / "babeldoc-cache"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from babeldoc.const import CACHE_FOLDER; print(CACHE_FOLDER)",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=_environment(str(cache)),
    )

    assert result.stdout.strip() == str(cache)
    assert (cache / "tiktoken").is_dir()


def test_relative_cache_home_fails_closed(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import babeldoc.const"],
        check=False,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_environment("relative-cache"),
    )

    assert result.returncode != 0
    assert "BABELDOC_CACHE_HOME must be an absolute path" in result.stderr
