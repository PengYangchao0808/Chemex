"""Compact public CLI for the single ChemEx-Lit pipeline."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Literal

import click

from chemex_lit import __version__
from chemex_lit.application import ChemExService
from chemex_lit.config import load_config
from chemex_lit.evaluation import evaluate_files
from chemex_lit.models import ReactionRecord, RunSummary
from chemex_lit.review import apply_corrections, generate_review
from chemex_lit.store import ArtifactStore


@click.group()
@click.option("--config", "config_path", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--verbose", is_flag=True, help="Enable debug logging.")
@click.version_option(__version__, prog_name="chemex-lit")
@click.pass_context
def main(ctx: click.Context, config_path: Path | None, verbose: bool) -> None:
    """Extract traceable chemical reaction records from literature PDFs."""

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="[%(asctime)s] %(levelname)-8s %(name)s | %(message)s",
    )
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


@main.command()
@click.argument("pdf_path", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--output-dir", "output_dir", type=click.Path(path_type=Path))
@click.option(
    "--mode",
    type=click.Choice(["auto", "semi", "agent"]),
    default="auto",
    show_default=True,
)
@click.option(
    "--structures",
    "external_structures",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help="Human-supplied StructureCandidate JSONL; replaces automatic candidates by label.",
)
@click.option("--adjudicate", is_flag=True, help="Adjudicate warning-only ambiguous records.")
@click.option("--json", "as_json", is_flag=True, help="Print the run summary as JSON.")
@click.pass_context
def run(
    ctx: click.Context,
    pdf_path: Path,
    output_dir: Path | None,
    mode: Literal["auto", "semi", "agent"],
    external_structures: Path | None,
    adjudicate: bool,
    as_json: bool,
) -> None:
    """Run the complete extraction pipeline."""

    summary = _service(ctx).run(
        pdf_path=pdf_path,
        output_dir=output_dir,
        mode=mode,
        external_structures=external_structures,
        adjudicate=adjudicate,
    )
    _print_run_summary(summary, as_json)
    if summary.status == "completed_empty":
        raise click.exceptions.Exit(4)


@main.command()
@click.argument("run_dir", type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def resume(ctx: click.Context, run_dir: Path, as_json: bool) -> None:
    """Resume a run from hash-validated artifacts."""

    summary = _service(ctx).resume(run_dir)
    _print_run_summary(summary, as_json)


@main.command()
@click.argument("run_dir", type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.argument(
    "files",
    nargs=-1,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
)
@click.option(
    "--kind",
    type=click.Choice(["candidates", "adjudications"]),
    default="candidates",
    show_default=True,
)
@click.option("--force", is_flag=True, help="Supersede previous submissions and invalidate downstream stages.")
@click.option("--resume", "resume_after_submit", is_flag=True, help="Resume immediately when the run becomes ready.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def submit(
    ctx: click.Context,
    run_dir: Path,
    files: tuple[Path, ...],
    kind: Literal["candidates", "adjudications"],
    force: bool,
    resume_after_submit: bool,
    as_json: bool,
) -> None:
    """Submit task-bound candidate or adjudication JSONL files."""

    if not files:
        raise click.UsageError("At least one submission file is required")
    service = _service(ctx)
    result = service.submit(run_dir, list(files), kind=kind, force=force)
    resumed: RunSummary | None = None
    if resume_after_submit and result["status"] == "ready":
        resumed = service.resume(run_dir)

    if as_json:
        payload: dict[str, Any] = dict(result)
        if resumed is not None:
            payload = {"submission": result, "resume": resumed.model_dump(mode="json")}
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    _print_submit_summary(result)
    if resumed is not None:
        click.echo("")
        _print_run_summary(resumed, False)


@main.command()
@click.argument("run_dir", type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def status(ctx: click.Context, run_dir: Path, as_json: bool) -> None:
    """Show persisted run and task status."""

    _print_status(_service(ctx).status(run_dir), as_json)


@main.command()
@click.argument("run_dir", type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def cancel(ctx: click.Context, run_dir: Path, as_json: bool) -> None:
    """Cancel an in-flight run."""

    service = _service(ctx)
    service.cancel(run_dir)
    _print_status(service.status(run_dir), as_json)


@main.command("review")
@click.argument("run_dir", type=click.Path(path_type=Path, exists=True, file_okay=False))
def review_command(run_dir: Path) -> None:
    """Regenerate the single HTML review dashboard."""

    store = ArtifactStore(run_dir)
    records = store.read_models("records.jsonl", ReactionRecord)
    output = generate_review(records, store.root / "review.html")
    click.echo(str(output))


@main.command("review-apply")
@click.argument("run_dir", type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.argument("corrections", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--confirmed-by", required=True, prompt=False)
def review_apply(run_dir: Path, corrections: Path, confirmed_by: str) -> None:
    """Apply explicit field corrections to records.corrected.jsonl."""

    store = ArtifactStore(run_dir)
    records = store.read_models("records.jsonl", ReactionRecord)
    corrected, audit_entries = apply_corrections(
        records,
        corrections,
        confirmed_by=confirmed_by,
    )
    output = store.write_jsonl("records.corrected.jsonl", corrected)
    audit_output = store.append_jsonl("audit.jsonl", audit_entries)
    click.echo(str(output))
    click.echo(str(audit_output))


@main.command("evaluate")
@click.argument("run_dir", type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.option("--gold", required=True, type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--output", type=click.Path(path_type=Path, dir_okay=False))
def evaluate_command(run_dir: Path, gold: Path, output: Path | None) -> None:
    """Evaluate records against a frozen JSONL benchmark."""

    predicted = run_dir / "records.jsonl"
    report_path = output or run_dir / "evaluation.json"
    report = evaluate_files(predicted, gold, report_path)
    click.echo(json.dumps(report, ensure_ascii=False, indent=2))


@main.command()
@click.option("--show-config", is_flag=True, help="Print non-secret effective configuration.")
@click.pass_context
def check(ctx: click.Context, show_config: bool) -> None:
    """Check local runtime dependencies and required credential variables."""

    config = load_config(ctx.obj.get("config_path"))
    checks: list[tuple[str, bool, str]] = []
    try:
        from rdkit import Chem

        checks.append(("RDKit", Chem.MolFromSmiles("CC") is not None, "import and parse"))
    except ImportError as exc:
        checks.append(("RDKit", False, str(exc)))
    for label, env_name in (
        ("MinerU key", config.mineru.api_key_env),
        ("Text model key", config.models.text.api_key_env),
        ("Vision model key", config.models.vision.api_key_env),
    ):
        checks.append((label, bool(os.environ.get(env_name)), env_name))
    for label, ok, detail in checks:
        click.echo(f"{'OK' if ok else 'MISSING':7} {label}: {detail}")
    if show_config:
        click.echo(json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2))
    if not all(ok for _, ok, _ in checks):
        raise click.exceptions.Exit(2)


def _service(ctx: click.Context) -> ChemExService:
    """Build a service instance from CLI context."""

    return ChemExService(load_config(ctx.obj.get("config_path")))


def _print_run_summary(summary: RunSummary, as_json: bool) -> None:
    """Print a run summary in human or JSON form."""

    payload = summary.model_dump(mode="json")
    if as_json:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    click.echo(f"Run: {summary.run_id}")
    click.echo(f"Status: {summary.status}")
    click.echo(f"Records: {summary.records_count}")
    click.echo(f"Needs review: {summary.review_count}")
    click.echo(f"Output: {summary.output_dir}")
    if summary.status == "awaiting_input":
        click.echo(f"Awaiting tasks: {len(summary.awaiting)}")
        click.echo(f"Task file: {Path(summary.output_dir) / 'tasks/extraction.jsonl'}")


def _print_submit_summary(summary: dict[str, Any]) -> None:
    """Print a submission summary in human form."""

    click.echo(f"Status: {summary['status']}")
    click.echo(f"Applied: {summary['applied']}")
    click.echo(f"Awaiting tasks: {len(summary['awaiting'])}")
    for file_info in summary.get("files", []):
        click.echo(
            f"  {file_info['status']}: {file_info['file']} ({file_info['sha256'][:12]})"
        )
    awaiting = summary.get("awaiting", [])
    for task_id in awaiting[:5]:
        click.echo(f"  awaiting: {task_id}")


def _print_status(status: dict[str, Any], as_json: bool) -> None:
    """Print run status data in human or JSON form."""

    if as_json:
        click.echo(json.dumps(status, ensure_ascii=False, indent=2))
        return
    click.echo(f"Run: {status.get('run_id')}")
    click.echo(f"Status: {status.get('status')}")
    for name, stage in status.get("stages", {}).items():
        click.echo(f"  {name}: {stage.get('status')} — {stage.get('detail', '')}")
    tasks = status.get("tasks", {})
    if tasks:
        click.echo("Tasks:")
        for kind, data in tasks.items():
            click.echo(
                f"  {kind}: awaiting={data.get('awaiting', 0)} fulfilled={data.get('fulfilled', 0)}"
            )
            for task_id in data.get("awaiting_task_ids", [])[:5]:
                click.echo(f"    awaiting: {task_id}")


if __name__ == "__main__":
    main()
