from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from tools.labctl.kube import CommandError, run_command, run_or_raise


@dataclass(frozen=True)
class StartConfig:
    cluster_name: str
    kind_config_path: Path


class StartError(RuntimeError):
    pass


def start_cluster(cfg: StartConfig) -> None:
    try:
        if _cluster_exists(cfg.cluster_name):
            if _cluster_is_healthy():
                _print_success(cfg.cluster_name, reused=True)
                return
            _recreate_cluster(cfg)
            _print_success(cfg.cluster_name, reused=False)
            return

        _create_cluster(cfg)
        _print_success(cfg.cluster_name, reused=False)
    except CommandError as e:
        msg = _format_command_error(e)
        raise StartError(msg) from e

def _expected_topology_from_kind_yaml(path: str | Path) -> tuple[int, int, int]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    nodes = data.get("nodes", []) or []

    total = len(nodes)
    control_planes = sum(1 for n in nodes if n.get("role") == "control-plane")
    workers = sum(1 for n in nodes if n.get("role") == "worker")

    return total, control_planes, workers

def _cluster_exists(cluster_name: str) -> bool:
    result = run_or_raise(["kind", "get", "clusters"])
    clusters = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return cluster_name in clusters


def _cluster_is_healthy() -> bool:
    info = run_command(["kubectl", "cluster-info"])
    if info.returncode != 0:
        return False

    # expected from kind config
    expected_total, expected_cp, expected_workers = _expected_topology_from_kind_yaml(
        "infra/kind/cluster.yaml"
    )

    # actual from kubectl
    nodes = run_command(["kubectl", "get", "nodes", "-o", "json"])
    if nodes.returncode != 0:
        return False

    payload = json.loads(nodes.stdout)
    items = payload.get("items", []) or []

    actual_total = len(items)

    actual_cp = 0
    actual_ready = 0
    for it in items:
        labels = (it.get("metadata", {}) or {}).get("labels", {}) or {}
        if "node-role.kubernetes.io/control-plane" in labels:
            actual_cp += 1

        conditions = (it.get("status", {}) or {}).get("conditions", []) or []
        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
            actual_ready += 1

    actual_workers = actual_total - actual_cp

    if actual_total != expected_total:
        return False
    if actual_cp != expected_cp:
        return False
    if actual_workers != expected_workers:
        return False
    if actual_ready != expected_total:
        return False

    return True

def _create_cluster(cfg: StartConfig) -> None:
    run_or_raise(
        [
            "kind",
            "create",
            "cluster",
            "--name",
            cfg.cluster_name,
            "--config",
            str(cfg.kind_config_path),
        ]
    )
    _verify_cluster_ready()


def _recreate_cluster(cfg: StartConfig) -> None:
    run_or_raise(["kind", "delete", "cluster", "--name", cfg.cluster_name])
    _create_cluster(cfg)


def _verify_cluster_ready() -> None:
    run_or_raise(["kubectl", "cluster-info"])
    run_or_raise(["kubectl", "get", "nodes", "-o", "wide"])


def _print_success(cluster_name: str, reused: bool) -> None:
    if reused:
        print(f"OK  cluster '{cluster_name}' already exists and is healthy")
    else:
        print(f"OK  cluster '{cluster_name}' is ready")

    nodes = run_or_raise(["kubectl", "get", "nodes", "-o", "wide"]).stdout.strip()
    if nodes:
        print(nodes)

    print("OK  you can continue with the next steps")


def _format_command_error(e: CommandError) -> str:
    cmd = " ".join(e.command)
    stderr = e.result.stderr.strip()
    stdout = e.result.stdout.strip()
    details = stderr if stderr else stdout
    details = details if details else "unknown error"
    return f"start failed while running: {cmd}\n{details}"
