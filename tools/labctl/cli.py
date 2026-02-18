from __future__ import annotations

import typer

from tools.labctl.doctor import run_doctor

app = typer.Typer(no_args_is_help=True)

@app.command()
def doctor() -> None:
    results = run_doctor()
    for r in results:
        print(f"{r.status.value} {r.name}: {r.message}")
    raise typer.Exit(code=0)

@app.command()
def levels() -> None:
    raise typer.Exit(code=0)

