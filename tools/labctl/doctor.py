from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class Status(str, Enum):
    OK = "OK"
    WARN = "WRN"
    ERR = "ERR"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    message: str


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _cmd_version(cmd: str, args: list[str]) -> str:
    proc = _run([cmd, *args])
    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    line = _first_line(out)
    return line if line else "version output not available"


def _check_command(cmd: str, version_args: list[str] | None = None) -> CheckResult:
    path = _which(cmd)
    if not path:
        return CheckResult(name=cmd, status=Status.ERR, message="not found")

    if version_args is None:
        return CheckResult(name=cmd, status=Status.OK, message=path)

    ver = _cmd_version(cmd, version_args)
    return CheckResult(name=cmd, status=Status.OK, message=f"{path} | {ver}")


def _check_docker_daemon() -> CheckResult:
    if not _which("docker"):
        return CheckResult(name="docker daemon", status=Status.ERR, message="docker not installed")

    proc = _run(["docker", "info"])
    if proc.returncode == 0:
        return CheckResult(name="docker daemon", status=Status.OK, message="reachable")
    msg = _first_line(proc.stderr) or _first_line(proc.stdout) or "not reachable (is Docker running?)"
    return CheckResult(name="docker daemon", status=Status.ERR, message=msg)


def _check_kubectl_kustomize() -> CheckResult:
    if not _which("kubectl"):
        return CheckResult(name="kubectl kustomize", status=Status.ERR, message="kubectl not installed")

    proc = _run(["kubectl", "kustomize", "--help"])
    if proc.returncode == 0:
        return CheckResult(name="kubectl kustomize", status=Status.OK, message="available")

    if _which("kustomize"):
        ver = _cmd_version("kustomize", ["version"])
        return CheckResult(name="kustomize", status=Status.OK, message=ver)

    return CheckResult(
        name="kustomize",
        status=Status.WARN,
        message="not detected (kubectl kustomize unavailable and kustomize binary not found)",
    )


def run_doctor() -> list[CheckResult]:
    checks: list[CheckResult] = [
        _check_command("git", ["--version"]),
        _check_command("python3", ["--version"]),
        _check_command("docker", ["--version"]),
        _check_docker_daemon(),
        _check_command("kubectl", ["version", "--client", "--short"]),
        _check_command("kind", ["version"]),
        _check_kubectl_kustomize(),
    ]
    return checks


def exit_code(results: Iterable[CheckResult]) -> int:
    for r in results:
        if r.status == Status.ERR:
            return 1
    return 0

