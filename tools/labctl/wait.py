from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class WaitSpec:
    poll_seconds: float = 2.0


CheckFn = Callable[[], tuple[bool, str]]
FailFastFn = Callable[[], Optional[str]]


class Waiter:
    def __init__(self, spec: WaitSpec):
        self.spec = spec

    def wait(self, title: str, check: CheckFn, fail_fast: FailFastFn | None = None) -> None:
        print(f"  → {title}...")

        while True:
            if fail_fast is not None:
                err = fail_fast()
                if err:
                    raise RuntimeError(err)

            done, msg = check()

            if msg:
                print(f"     {msg}", end="\r")

            if done:
                if msg:
                    print(f"     {msg} (OK)            ")
                else:
                    print("     OK")
                return

            time.sleep(self.spec.poll_seconds)