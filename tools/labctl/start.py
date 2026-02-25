from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import yaml

from .config import LabConfig, kind_cluster_config_path, repo_root
from .kube import CommandError, run_command, run_or_raise
from .wait import WaitSpec, Waiter


class StartError(Exception):
    pass


class StartService:
    def __init__(self, config: LabConfig, wait: WaitSpec | None = None):
        self.config = config
        self.wait = wait or WaitSpec()
        self.waiter = Waiter(self.wait)

    def execute(self) -> None:
        print("\n[1/6] Reconciling cluster...")
        self._ensure_cluster()

        print("[2/6] Waiting for nodes to become Ready...")
        self.waiter.wait("Nodes becoming Ready", self._check_nodes_ready)

        print("[3/6] Applying base manifests (kubectl apply -k)...")
        self._apply_base_manifests()

        print("[4/6] Waiting for core system components (CoreDNS)...")
        self.waiter.wait(
            "CoreDNS deployment Available",
            lambda: self._check_deployment_available(
                namespace="kube-system",
                deployment="coredns",
                component_name="CoreDNS",
            ),
            fail_fast=lambda: self._fail_fast_pods(
                namespace="kube-system",
                label_selector="k8s-app=kube-dns",
                component_name="CoreDNS",
            ),
        )

        print("[5/6] Waiting for ingress platform (ingress-nginx)...")
        self.waiter.wait(
            "Ingress controller Pods Ready",
            self._check_ingress_controller_pods_ready,
            fail_fast=lambda: self._fail_fast_pods(
                namespace="ingress-nginx",
                label_selector="app.kubernetes.io/component=controller",
                component_name="ingress-nginx controller",
            ),
        )

        print("[6/6] Running ingress entrypoint smoke test (localhost:8080)...")
        self.waiter.wait("Ingress 200 OK (Host: hello.local)", self._check_ingress_smoke_200)

        print("\n✔ Cluster and platform are ready.\n")

 
    def _ensure_cluster(self) -> None:
        clusters = run_command(["kind", "get", "clusters"]).stdout.splitlines()

        if self.config.cluster_name not in clusters:
            print("  → Cluster not found. Creating...")
            self._create_cluster()
            print("  → Cluster created.")
            return

        if not self._cluster_is_healthy_snapshot():
            print("  → Existing cluster unhealthy. Recreating...")
            self._delete_cluster()
            self._create_cluster()
            print("  → Cluster recreated.")
        else:
            print("  → Existing cluster is healthy. Reusing.")

    def _create_cluster(self) -> None:
        try:
            run_or_raise(
                [
                    "kind",
                    "create",
                    "cluster",
                    "--name",
                    self.config.cluster_name,
                    "--config",
                    str(kind_cluster_config_path()),
                ]
            )
        except CommandError as e:
            raise StartError(f"Failed to create cluster: {e}") from e

    def _delete_cluster(self) -> None:
        try:
            run_or_raise(["kind", "delete", "cluster", "--name", self.config.cluster_name])
        except CommandError as e:
            raise StartError(f"Failed to delete cluster: {e}") from e

    def _cluster_is_healthy_snapshot(self) -> bool:
        try:
            run_or_raise(["kubectl", "cluster-info"])
        except CommandError:
            return False

        try:
            result = run_or_raise(["kubectl", "get", "nodes", "-o", "json"])
            nodes = json.loads(result.stdout)
        except Exception:
            return False

        expected_nodes = self._expected_node_count()
        items = nodes.get("items", [])
        if len(items) != expected_nodes:
            return False

        return all(self._node_is_ready(n) for n in items)

    def _expected_node_count(self) -> int:
        config_path = kind_cluster_config_path()
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return len(data.get("nodes", []))

    def _node_is_ready(self, node_obj: dict) -> bool:
        conditions = node_obj.get("status", {}).get("conditions", [])
        return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


    def _apply_base_manifests(self) -> None:
        base_path = repo_root() / "infra" / "base"
        if not base_path.exists():
            raise StartError("infra/base directory not found.")

        try:
            run_or_raise(["kubectl", "apply", "-k", str(base_path)])
        except CommandError as e:
            raise StartError(f"Failed to apply base manifests: {e}") from e

        print("  → Base manifests applied.")

    def _check_nodes_ready(self) -> tuple[bool, str]:
        expected = self._expected_node_count()

        try:
            result = run_or_raise(["kubectl", "get", "nodes", "-o", "json"])
            nodes = json.loads(result.stdout)
            items = nodes.get("items", [])
        except Exception:
            return False, f"Nodes Ready: 0/{expected}"

        ready = sum(1 for n in items if self._node_is_ready(n))
        done = len(items) == expected and ready == expected

        return done, f"Nodes Ready: {ready}/{expected}"

    def _check_deployment_available(self, namespace: str, deployment: str, component_name: str) -> tuple[bool, str]:
        try:
            result = run_or_raise(["kubectl", "-n", namespace, "get", "deployment", deployment, "-o", "json"])
            dep = json.loads(result.stdout)
        except Exception:
            return False, f"{component_name}: deployment not found yet"

        desired = dep.get("spec", {}).get("replicas", 1) or 1
        available = dep.get("status", {}).get("availableReplicas", 0) or 0
        ready = dep.get("status", {}).get("readyReplicas", 0) or 0

        done = ready >= 1 and available >= 1

        return done, f"{component_name}: desired={desired} ready={ready} available={available}"

    def _check_ingress_controller_pods_ready(self) -> tuple[bool, str]:
        namespace = "ingress-nginx"
        label_selector = "app.kubernetes.io/component=controller"

        result = run_command(["kubectl", "-n", namespace, "get", "pods", "-l", label_selector, "-o", "json"])
        if result.returncode != 0:
            return False, "Ingress pods not found yet"

        data = json.loads(result.stdout)
        pods = data.get("items", [])
        if not pods:
            return False, "Ingress pods not created yet"

        ready_count = 0
        total = len(pods)

        for pod in pods:
            conditions = pod.get("status", {}).get("conditions", [])
            is_ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
            if is_ready:
                ready_count += 1

        done = ready_count == total
        return done, f"Ingress Pods Ready: {ready_count}/{total}"

    def _check_ingress_smoke_200(self) -> tuple[bool, str]:
        url = "http://127.0.0.1:8080/"
        host_header = "hello.local"

        try:
            req = urllib.request.Request(url, headers={"Host": host_header}, method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                status = resp.status
        except Exception:
            return False, "Ingress HTTP: no response yet"

        if status == 200:
            return True, "Ingress HTTP: 200 OK"

        return False, f"Ingress HTTP: {status}"


    def _fail_fast_pods(self, namespace: str, label_selector: str, component_name: str) -> str | None:
        pods = run_command(["kubectl", "-n", namespace, "get", "pods", "-l", label_selector, "-o", "json"])
        if pods.returncode != 0:
            return None

        pod_data = json.loads(pods.stdout)
        for pod in pod_data.get("items", []):
            pod_name = pod.get("metadata", {}).get("name", "<unknown>")

            for cs in pod.get("status", {}).get("containerStatuses", []) or []:
                state = cs.get("state", {}) or {}
                waiting = state.get("waiting")
                if not waiting:
                    continue

                reason = waiting.get("reason")
                message = waiting.get("message", "")

                if reason in ("ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff"):
                    return f"{component_name} pod failure: {pod_name} reason={reason} {message}".strip()

        return None