"""Compact public CLI for the single ChemEx-Lit pipeline."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import click

from chemex_lit import __version__
from chemex_lit.adjudicator import Adjudicator
from chemex_lit.assembly import Assembler
from chemex_lit.chemistry import Validator
from chemex_lit.config import AppConfig, load_config
from chemex_lit.evaluation import evaluate_files
from chemex_lit.extraction.structure import StructureExtractor
from chemex_lit.extraction.table import TableExtractor
from chemex_lit.extraction.text import TextExtractor
from chemex_lit.llm import LLMClient, PromptRegistry
from chemex_lit.mineru import MinerUAdapter
from chemex_lit.models import ReactionRecord, RunRequest
from chemex_lit.pipeline import Pipeline
from chemex_lit.review import apply_corrections, generate_review
from chemex_lit.store import ArtifactStore, sha256_file


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
    external_structures: Path | None,
    adjudicate: bool,
    as_json: bool,
) -> None:
    """Run the complete extraction pipeline."""
    config = load_config(ctx.obj.get("config_path"))
    if adjudicate:
        config = config.model_copy(
            update={
                "pipeline": config.pipeline.model_copy(update={"adjudicate_ambiguous": True})
            }
        )
    run_dir = output_dir or _default_run_dir(config, pdf_path)
    pipeline = build_pipeline(config)
    summary = pipeline.run(
        RunRequest(
            pdf_path=pdf_path,
            output_dir=run_dir,
            external_structures=external_structures,
        )
    )
    _print_summary(summary.model_dump(mode="json"), as_json)
    if summary.status == "completed_empty":
        raise click.exceptions.Exit(4)


@main.command()
@click.argument("run_dir", type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.option(
    "--structures",
    "external_structures",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def resume(
    ctx: click.Context,
    run_dir: Path,
    external_structures: Path | None,
    as_json: bool,
) -> None:
    """Resume a run from hash-validated artifacts."""
    store = ArtifactStore(run_dir)
    manifest = store.manifest()
    config = load_config(ctx.obj.get("config_path"))
    summary = build_pipeline(config).run(
        RunRequest(
            pdf_path=Path(manifest["input_path"]),
            output_dir=run_dir,
            external_structures=external_structures,
            resume=True,
        )
    )
    _print_summary(summary.model_dump(mode="json"), as_json)


@main.command()
@click.argument("run_dir", type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.option("--json", "as_json", is_flag=True)
def status(run_dir: Path, as_json: bool) -> None:
    """Show persisted run and stage status."""
    manifest = ArtifactStore(run_dir).manifest()
    if as_json:
        click.echo(json.dumps(manifest, ensure_ascii=False, indent=2))
        return
    click.echo(f"Run: {manifest.get('run_id')}")
    click.echo(f"Status: {manifest.get('status')}")
    for name, stage in manifest.get("stages", {}).items():
        click.echo(f"  {name}: {stage.get('status')} — {stage.get('detail', '')}")


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
    audit_path = store.root / "audit.jsonl"
    persisted_audit_entries: list[dict[str, Any]] = []
    if audit_path.is_file():
        persisted_audit_entries = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    audit_output = store.write_jsonl("audit.jsonl", [*persisted_audit_entries, *audit_entries])
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


def build_pipeline(config: AppConfig) -> Pipeline:
    """Construct the production pipeline without a service locator."""
    llm = LLMClient()
    prompts = PromptRegistry()
    reasoning_model, used_reasoning_fallback = config.models.reasoning_spec()
    if config.pipeline.adjudicate_ambiguous and used_reasoning_fallback:
        logging.getLogger(__name__).warning(
            "Reasoning model tier not configured; falling back to text model %s",
            reasoning_model.model,
        )
    adjudicator = (
        Adjudicator(llm, prompts, reasoning_model)
        if config.pipeline.adjudicate_ambiguous
        else None
    )
    return Pipeline(
        config=config,
        prompts=prompts,
        mineru=MinerUAdapter(config.mineru),
        text_extractor=TextExtractor(
            llm,
            prompts,
            config.models.text,
            config.pipeline.max_text_chars,
        ),
        table_extractor=TableExtractor(llm, prompts, config.models.text),
        structure_extractor=StructureExtractor(
            llm,
            prompts,
            config.models.vision,
            config.pipeline.image_workers,
        ),
        validator=Validator(),
        assembler=Assembler(),
        adjudicator=adjudicator,
    )


def _default_run_dir(config: AppConfig, pdf_path: Path) -> Path:
    return config.output_dir / f"{pdf_path.stem}-{sha256_file(pdf_path)[:8]}"


def _print_summary(summary: dict[str, Any], as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    click.echo(f"Run: {summary['run_id']}")
    click.echo(f"Status: {summary['status']}")
    click.echo(f"Records: {summary['records_count']}")
    click.echo(f"Needs review: {summary['review_count']}")
    click.echo(f"Output: {summary['output_dir']}")


if __name__ == "__main__":
    main()
