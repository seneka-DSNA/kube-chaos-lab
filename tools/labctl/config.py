from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LabConfig:
    cluster_name: str


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def lab_config() -> LabConfig:
    return LabConfig(cluster_name="kube-chaos-lab")


def kind_cluster_config_path() -> Path:
    return repo_root() / "infra" / "kind" / "cluster.yaml"
