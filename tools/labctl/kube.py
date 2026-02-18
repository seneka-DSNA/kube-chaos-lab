from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandError(RuntimeError):
    def __init__(self, command: Sequence[str], result: CommandResult) -> None:
        self.command = list(command)
        self.result = result
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        cmd = " ".join(self.command)
        details = self.result.stderr.strip() or self.result.stdout.strip() or "unknown error"
        return f"Command failed: {cmd}\n{details}"


def run_command(command: Sequence[str]) -> CommandResult:
    proc = subprocess.run(
        list(command),
        check=False,
        text=True,
        capture_output=True,
    )
    return CommandResult(
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def run_or_raise(command: Sequence[str]) -> CommandResult:
    result = run_command(command)
    if result.returncode != 0:
        raise CommandError(command, result)
    return result
