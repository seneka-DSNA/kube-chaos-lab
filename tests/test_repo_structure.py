from __future__ import annotations

from pathlib import Path


def test_required_paths_exist() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    required_paths = [
        repo_root / "pyproject.toml",
        repo_root / "README.md",
        repo_root / "docs" / "prerequisites.md",
        repo_root / "tools" / "__init__.py",
        repo_root / "tools" / "labctl" / "__init__.py",
        repo_root / "tools" / "labctl" / "cli.py",
        repo_root / "tools" / "labctl" / "doctor.py",
    ]

    missing = [str(p) for p in required_paths if not p.exists()]
    assert not missing, f"Missing required paths: {missing}"
