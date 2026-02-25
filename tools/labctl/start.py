from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import yaml

from .config import LabConfig, kind_cluster_config_path, repo_root
from .kube import CommandError, run_command, run_or_raise


class StartError(Exception):
    pass


@dataclass(frozen=True)
class WaitSpec:
    poll_seconds: float = 2.0


class StartService:
    def __init__(self, config: LabConfig, wait: WaitSpec | None = None):
        self.config = config
        self.wait = wait or WaitSpec()

    def execute(self) -> None:
        print("\n[1/6] Reconciling cluster...")
        self._ensure_cluster()

        print("[2/6] Waiting for nodes to become Ready...")
        self._wait_for_nodes_ready()

        print("[3/6] Applying base manifests (kubectl apply -k)...")
        self._apply_base_manifests()

        print("[4/6] Waiting for core system components (CoreDNS)...")
        self._wait_for_deployment_available(
            namespace="kube-system",
            deployment="coredns",
            pod_label_selector="k8s-app=kube-dns",
            component_name="CoreDNS",
        )

        print("[5/6] Waiting for ingress platform (ingress-nginx)...")
        self._wait_for_platform_ingress()

        print("[6/6] Running ingress entrypoint smoke test (localhost:8080)...")
        self._smoke_test_ingress_entrypoint()

        print("\n✔ Cluster and platform are ready.\n")

    # ------------------------------------------------------------
    # Cluster reconciliation
    # ------------------------------------------------------------

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

    # ------------------------------------------------------------
    # Snapshot health (fast)
    # ------------------------------------------------------------

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

        for node in items:
            if not self._node_is_ready(node):
                return False

        return True

    def _expected_node_count(self) -> int:
        config_path = kind_cluster_config_path()
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return len(data.get("nodes", []))

    def _node_is_ready(self, node_obj: dict) -> bool:
        conditions = node_obj.get("status", {}).get("conditions", [])
        return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)

    def _wait_for_nodes_ready(self) -> None:
        expected = self._expected_node_count()

        while True:
            try:
                result = run_or_raise(["kubectl", "get", "nodes", "-o", "json"])
                nodes = json.loads(result.stdout)
                items = nodes.get("items", [])
            except Exception:
                time.sleep(self.wait.poll_seconds)
                continue

            ready = sum(1 for n in items if self._node_is_ready(n))
            print(f"  → Nodes Ready: {ready}/{expected}", end="\r")

            if len(items) == expected and ready == expected:
                print("  → Nodes Ready: {}/{} (OK)           ".format(ready, expected))
                return

            time.sleep(self.wait.poll_seconds)

    # ------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------

    def _apply_base_manifests(self) -> None:
        base_path = repo_root() / "infra" / "base"
        if not base_path.exists():
            raise StartError("infra/base directory not found.")

        try:
            run_or_raise(["kubectl", "apply", "-k", str(base_path)])
        except CommandError as e:
            raise StartError(f"Failed to apply base manifests: {e}") from e

        print("  → Base manifests applied.")

    # ------------------------------------------------------------
    # Generic readiness: wait until deployment is Available (no timeout)
    # plus failure-state detection on pods.
    # ------------------------------------------------------------

    def _wait_for_deployment_available(
        self,
        namespace: str,
        deployment: str,
        pod_label_selector: str,
        component_name: str,
    ) -> None:
        print(f"  → Waiting for {component_name} to become Available...")

        while True:
            dep = self._get_deployment_json(namespace, deployment)
            if dep is None:
                time.sleep(self.wait.poll_seconds)
                continue

            desired = dep.get("status", {}).get("replicas", 0) or 0
            available = dep.get("status", {}).get("availableReplicas", 0) or 0
            updated = dep.get("status", {}).get("updatedReplicas", 0) or 0

            print(
                f"     Replicas: desired={desired} updated={updated} available={available}",
                end="\r",
            )

            if desired > 0 and available == desired:
                print(
                    f"\n  → {component_name} Available (replicas {available}/{desired})."
                )
                return

            self._raise_on_terminal_pod_failures(
                namespace=namespace,
                label_selector=pod_label_selector,
                component_name=component_name,
            )

            time.sleep(self.wait.poll_seconds)

    def _get_deployment_json(self, namespace: str, deployment: str) -> dict | None:
        try:
            result = run_or_raise(["kubectl", "-n", namespace, "get", "deployment", deployment, "-o", "json"])
            return json.loads(result.stdout)
        except Exception:
            return None

    def _raise_on_terminal_pod_failures(self, namespace: str, label_selector: str, component_name: str) -> None:
        pods = run_command(
            ["kubectl", "-n", namespace, "get", "pods", "-l", label_selector, "-o", "json"]
        )
        if pods.returncode != 0:
            return

        pod_data = json.loads(pods.stdout)
        for pod in pod_data.get("items", []):
            pod_name = pod.get("metadata", {}).get("name", "<unknown>")
            phase = pod.get("status", {}).get("phase", "<unknown>")

            # Detect container waiting reasons that are effectively "terminal until action is taken"
            for cs in pod.get("status", {}).get("containerStatuses", []) or []:
                state = cs.get("state", {}) or {}
                waiting = state.get("waiting")
                if not waiting:
                    continue

                reason = waiting.get("reason")
                message = waiting.get("message", "")

                if reason in ("ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff"):
                    raise StartError(
                        f"{component_name} pod failure: {pod_name} phase={phase} reason={reason} {message}".strip()
                    )

    # ------------------------------------------------------------
    # Ingress platform gates (no timeout)
    # ------------------------------------------------------------

    def _wait_for_platform_ingress(self) -> None:
        namespace = "ingress-nginx"
        label_selector = "app.kubernetes.io/component=controller"

        print("  → Waiting for ingress-nginx controller Pods to be Ready...")

        while True:
            result = run_command(
                [
                    "kubectl",
                    "-n",
                    namespace,
                    "get",
                    "pods",
                    "-l",
                    label_selector,
                    "-o",
                    "json",
                ]
            )

            if result.returncode != 0:
                print("     Pods not found yet...", end="\r")
                time.sleep(self.wait.poll_seconds)
                continue

            data = json.loads(result.stdout)
            pods = data.get("items", [])

            if not pods:
                print("     No ingress controller pods yet...", end="\r")
                time.sleep(self.wait.poll_seconds)
                continue

            ready_count = 0
            total = len(pods)

            for pod in pods:
                conditions = pod.get("status", {}).get("conditions", [])
                is_ready = any(
                    c.get("type") == "Ready" and c.get("status") == "True"
                    for c in conditions
                )
                if is_ready:
                    ready_count += 1

                # fallo terminal inmediato
                for cs in pod.get("status", {}).get("containerStatuses", []) or []:
                    state = cs.get("state", {})
                    waiting = state.get("waiting")
                    if waiting:
                        reason = waiting.get("reason")
                        if reason in (
                            "ImagePullBackOff",
                            "ErrImagePull",
                            "CrashLoopBackOff",
                        ):
                            raise StartError(
                                f"Ingress pod {pod['metadata']['name']} failure: {reason}"
                            )

            print(f"     Pods Ready: {ready_count}/{total}", end="\r")

            if ready_count == total:
                print(f"\n  → ingress-nginx controller Pods Ready ({ready_count}/{total}).")
                return

            time.sleep(self.wait.poll_seconds)

    def _wait_for_job_complete_if_exists(self, namespace: str, job_name: str) -> None:
        exists = run_command(["kubectl", "-n", namespace, "get", "job", job_name]).returncode == 0
        if not exists:
            return

        print(f"  → Waiting for Job {namespace}/{job_name} to complete...")

        while True:
            job = self._get_job_json(namespace, job_name)
            if job is None:
                time.sleep(self.wait.poll_seconds)
                continue

            status = job.get("status", {}) or {}
            succeeded = status.get("succeeded", 0) or 0
            failed = status.get("failed", 0) or 0
            active = status.get("active", 0) or 0

            print(
                f"     Job status: succeeded={succeeded} active={active} failed={failed}",
                end="\r",
            )

            if succeeded >= 1:
                print(f"\n  → Job {namespace}/{job_name} Completed.")
                return

            if failed and failed > 0:
                raise StartError(f"Job {namespace}/{job_name} failed (failed={failed}).")

            time.sleep(self.wait.poll_seconds)

    def _get_job_json(self, namespace: str, job_name: str) -> dict | None:
        try:
            result = run_or_raise(["kubectl", "-n", namespace, "get", "job", job_name, "-o", "json"])
            return json.loads(result.stdout)
        except Exception:
            return None

    def _wait_for_service_endpoints(self, namespace: str, service_name: str) -> None:
        print(f"  → Waiting for endpoints for Service {namespace}/{service_name}...")

        while True:
            try:
                result = run_or_raise(["kubectl", "-n", namespace, "get", "endpoints", service_name, "-o", "json"])
                ep = json.loads(result.stdout)
            except Exception:
                time.sleep(self.wait.poll_seconds)
                continue

            subsets = ep.get("subsets") or []
            addresses = 0
            for s in subsets:
                addresses += len(s.get("addresses") or [])

            print(f"     Endpoint addresses: {addresses}", end="\r")

            if addresses > 0:
                print(f"\n  → Service {namespace}/{service_name} has endpoints (OK).")
                return

            time.sleep(self.wait.poll_seconds)

    # ------------------------------------------------------------
    # Smoke test (no timeout): abort only on explicit user interrupt
    # ------------------------------------------------------------

    def _smoke_test_ingress_entrypoint(self) -> None:
        url = "http://127.0.0.1:8080/"
        host_header = "hello.local"

        attempt = 0

        print(f"  → Smoke testing ingress with Host: {host_header}")

        while True:
            attempt += 1

            try:
                req = urllib.request.Request(
                    url,
                    headers={"Host": host_header},
                    method="GET",
                )

                with urllib.request.urlopen(req, timeout=2) as resp:
                    status = resp.status

                    # Consider 200 OK as success (real E2E validation)
                    if status == 200:
                        print("  → Ingress entrypoint responding with 200 OK.")
                        return

                    print(
                        f"  → Ingress responded with HTTP {status}, waiting...",
                        end="\r",
                    )

            except (urllib.error.URLError, TimeoutError, ConnectionError):
                print(
                    f"  → Waiting for ingress entrypoint... (attempt {attempt})",
                    end="\r",
                )

            except Exception as e:
                print(
                    f"  → Waiting for ingress entrypoint... (attempt {attempt})",
                    end="\r",
                )

            time.sleep(self.wait.poll_seconds)