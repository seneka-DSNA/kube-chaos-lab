from __future__ import annotations

import subprocess
import sys


def test_labctl_help_runs() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "tools.labctl.cli", "--help"],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
