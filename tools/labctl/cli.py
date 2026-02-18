from __future__ import annotations

import typer

from tools.labctl.config import kind_cluster_config_path, lab_config
from tools.labctl.doctor import run_doctor
from tools.labctl.start import StartConfig, StartError, start_cluster

app = typer.Typer(no_args_is_help=True)

@app.command()
def doctor() -> None:
    results = run_doctor()
    for r in results:
        print(f"{r.status.value} {r.name}: {r.message}")
    raise typer.Exit(code=0)
@app.command()
def start() -> None:
    cfg = lab_config()
    start_cfg = StartConfig(
                     cluster_name=cfg.cluster_name,
                     kind_config_path=kind_cluster_config_path()
                     )
    try:
        start_cluster(start_cfg)
    except StartError as e:
        print(f"ERROR: {e}")
        raise typer.Exit(code=1)
@app.command()
def levels() -> None:
    raise typer.Exit(code=0)

