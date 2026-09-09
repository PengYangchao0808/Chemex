"""Compact public CLI for the single ChemEx-Lit pipeline."""

from __future__ import annotations

import getpass
import io
import json
import logging
from pathlib import Path
from typing import Callable, Literal, NotRequired, TypedDict, cast

import click
import httpx

from chemex_lit import __version__
from chemex_lit.config import AppConfig, load_config
from chemex_lit import profiles
from chemex_lit.credentials import (
    EXPIRING_SOON_DAYS,
    auth_store_path,
    check_credential_status,
    credential_value,
    expires_in_days,
    load_auth_store,
    remove_credential,
    resolve_credential,
    set_credential,
    validate_credential_name,
)
from chemex_lit.evaluation import evaluate_files
from chemex_lit.errors import ChemExError
from chemex_lit.models import ReactionRecord, RunSummary, normalize_mode
from chemex_lit.pipeline import resume_run, run_pdf, run_status, submit_files
from chemex_lit.review import apply_corrections, generate_review
from chemex_lit.store import ArtifactStore, atomic_write_text


class CliContext(TypedDict):
    """Typed context object shared by CLI commands."""

    config_path: Path | None
    profile: str | None
    models_path: Path | None


class SubmitFileInfo(TypedDict):
    """Human-readable metadata for one submitted file."""

    file: str
    sha256: str
    status: str


class SubmitSummary(TypedDict):
    """Submit command summary emitted by the pipeline workflow API."""

    status: str
    applied: int
    awaiting: list[str]
    files: NotRequired[list[SubmitFileInfo]]


_MODE_CHOICES = ("auto", "semi", "agent")
_MODE_HELP = (
    "Run mode: auto is unattended, semi externalizes structures, "
    "agent externalizes all generative tasks."
)


@click.group()
@click.option(
    "--profile",
    type=str,
    default=None,
    help="Named profile from ~/.config/chemex-lit/models.yaml.",
)
@click.option(
    "--models-path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Explicit models.yaml path; defaults to the user config directory.",
)
@click.option("--config", "config_path", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--verbose", is_flag=True, help="Enable debug logging.")
@click.version_option(__version__, prog_name="chemex-lit")
@click.pass_context
def main(
    ctx: click.Context,
    profile: str | None,
    models_path: Path | None,
    config_path: Path | None,
    verbose: bool,
) -> None:
    """Extract traceable chemical reaction records from literature PDFs."""

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="[%(asctime)s] %(levelname)-8s %(name)s | %(message)s",
    )
    obj = _ctx_obj(ctx)
    obj["profile"] = profile
    obj["models_path"] = models_path
    obj["config_path"] = config_path


@main.command()
@click.argument("pdf_path", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--output-dir", "output_dir", type=click.Path(path_type=Path))
@click.option(
    "--mode",
    type=click.Choice(_MODE_CHOICES),
    default="auto",
    show_default=True,
    help=_MODE_HELP,
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
    mode: str,
    external_structures: Path | None,
    adjudicate: bool,
    as_json: bool,
) -> None:
    """Run the complete extraction pipeline."""

    summary = run_pdf(
        _config(ctx),
        pdf_path=pdf_path,
        output_dir=output_dir,
        mode=normalize_mode(mode),
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

    summary = resume_run(_config(ctx), run_dir)
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
    result = cast(
        SubmitSummary,
        cast(object, submit_files(run_dir, list(files), kind=kind, force=force)),
    )
    resumed: RunSummary | None = None
    if resume_after_submit and result["status"] == "ready":
        resumed = resume_run(_config(ctx), run_dir)

    if as_json:
        payload: object = result
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

    _print_status(run_status(run_dir), as_json)


@main.command()
@click.argument("run_dir", type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def cancel(ctx: click.Context, run_dir: Path, as_json: bool) -> None:
    """Cancel an in-flight run."""

    del ctx
    store = ArtifactStore(run_dir)
    manifest = store.manifest()
    status = str(manifest.get("status", ""))
    if status not in {"running", "awaiting_input", "ready"}:
        raise ChemExError(f"Run cannot be cancelled from terminal status: {status}")
    store.finish("cancelled")
    _print_status(run_status(run_dir), as_json)


@main.command("review")
@click.argument("run_dir", type=click.Path(path_type=Path, exists=True, file_okay=False))
def review_command(run_dir: Path) -> None:
    """Regenerate the single HTML review dashboard."""

    store = ArtifactStore(run_dir)
    records = store.read_models("records.jsonl", ReactionRecord)
    output = generate_review(records, store)
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

    store = ArtifactStore(run_dir)
    predicted = run_dir / "records.jsonl"
    if output is None:
        report = evaluate_files(predicted, gold, store)
    else:
        report = evaluate_files(predicted, gold)
        atomic_write_text(output, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    click.echo(json.dumps(report, ensure_ascii=False, indent=2))


@main.command()
@click.option("--show-config", is_flag=True, help="Print non-secret effective configuration.")
@click.option(
    "--mode",
    type=click.Choice(_MODE_CHOICES),
    default="auto",
    show_default=True,
    help=(
        "Mode whose CLI credential requirements are checked; "
        "auto and semi need text/vision keys, agent needs only MinerU."
    ),
)
@click.option("--json", "as_json", is_flag=True, help="Print the check result as JSON.")
@click.pass_context
def check(ctx: click.Context, show_config: bool, mode: str, as_json: bool) -> None:
    """Check local runtime dependencies and required credential variables."""

    obj = _ctx_obj(ctx)
    config = load_config(
        obj.get("config_path"),
        profile=obj.get("profile"),
        models_path=obj.get("models_path"),
    )
    run_mode = normalize_mode(mode)
    checks: list[dict[str, object]] = []
    try:
        from rdkit import Chem

        parser = cast(
            Callable[[str], object | None] | None,
            getattr(Chem, "MolFromSmiles", None),
        )
        checks.append(
            {
                "name": "RDKit",
                "ok": parser is not None and parser("CC") is not None,
                "detail": "import and parse",
            }
        )
    except ImportError as exc:
        checks.append({"name": "RDKit", "ok": False, "detail": str(exc)})
    required: list[tuple[str, str]] = [("MinerU key", config.mineru.api_key_env)]
    if run_mode in {"auto", "semi"}:
        required.extend(
            [
                ("Text model key", config.models.text.api_key_env),
                ("Vision model key", config.models.vision.api_key_env),
            ]
        )
    for label, env_name in required:
        present, source, days = check_credential_status(env_name)
        status = "ok" if present else "missing"
        detail = env_name
        if present and days is not None:
            if days < EXPIRING_SOON_DAYS:
                status = "expiring"
            detail = f"{env_name} (expires in {days}d)" if days >= 0 else f"{env_name} (expired)"
        checks.append(
            {
                "name": label,
                "ok": present,
                "detail": detail,
                "status": status,
                "source": source,
                "expires_in_days": days,
            }
        )
    ok = all(row["ok"] for row in checks)
    if as_json:
        payload = {
            "ok": ok,
            "mode": run_mode,
            "profile": config.profile,
            "checks": [
                {
                    "name": row["name"],
                    "status": row.get("status", "ok" if row["ok"] else "missing"),
                    **(
                        {"source": row["source"], "expires_in_days": row["expires_in_days"]}
                        if "source" in row
                        else {}
                    ),
                }
                for row in checks
            ],
        }
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        if show_config:
            click.echo(json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2))
        if not ok:
            raise click.exceptions.Exit(2)
        return
    for row in checks:
        click.echo(f"{'OK' if row['ok'] else 'MISSING':7} {row['name']}: {row['detail']}")
    click.echo(f"Profile: {config.profile if config.profile else '(none)'}")
    click.echo(f"Source:  {config.models_source}")
    click.echo(f"Mode:    {run_mode}")
    if show_config:
        click.echo(json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2))
    if not ok:
        raise click.exceptions.Exit(2)


@main.group(name="models")
@click.pass_context
def models(ctx: click.Context) -> None:
    """Inspect named model profiles."""

    ctx.ensure_object(dict)


def _profiles_for_cli(ctx: click.Context) -> tuple[profiles.ProfilesFile, bool]:
    """Load the explicit/user profiles, falling back to packaged samples."""

    obj = _ctx_obj(ctx)
    models_file = profiles.load_profiles_file(obj.get("models_path"))
    if models_file is not None:
        return models_file, False
    return profiles.packaged_profiles(), True


@models.command("list")
@click.pass_context
def models_list(ctx: click.Context) -> None:
    """List available named model profiles."""

    profiles_file, packaged = _profiles_for_cli(ctx)
    if packaged:
        click.echo(
            f"# (no user models.yaml at {profiles.default_models_path()}; showing packaged samples)"
        )
    for name in sorted(profiles_file.profiles):
        suffix = " *" if profiles_file.default_profile == name else ""
        click.echo(f"{name}{suffix}")


@models.command("show")
@click.argument("name")
@click.pass_context
def models_show(ctx: click.Context, name: str) -> None:
    """Show one named model profile as JSON."""

    profiles_file, _ = _profiles_for_cli(ctx)
    if name not in profiles_file.profiles:
        raise click.UsageError(
            f"Unknown profile: {name}. Available: {sorted(profiles_file.profiles)}."
        )
    click.echo(
        json.dumps(
            profiles_file.profiles[name].model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
    )


@models.command("check")
@click.argument("name")
@click.pass_context
def models_check(ctx: click.Context, name: str) -> None:
    """Check whether a profile's credential env vars are set."""

    profiles_file, _ = _profiles_for_cli(ctx)
    if name not in profiles_file.profiles:
        raise click.UsageError(
            f"Unknown profile: {name}. Available: {sorted(profiles_file.profiles)}."
        )

    profile = profiles_file.profiles[name]
    env_names = [profile.text.api_key_env, profile.vision.api_key_env]
    if profile.reasoning is not None and profile.reasoning.api_key_env not in env_names:
        env_names.append(profile.reasoning.api_key_env)

    missing = False
    for env_name in env_names:
        if resolve_credential(env_name) is not None:
            click.echo(f"OK {env_name}")
            continue
        click.echo(f"MISSING {env_name}")
        missing = True
    if missing:
        raise click.exceptions.Exit(2)


def _config(ctx: click.Context) -> AppConfig:
    """Load the effective configuration from CLI context."""

    obj = _ctx_obj(ctx)
    return load_config(
        obj.get("config_path"),
        profile=obj.get("profile"),
        models_path=obj.get("models_path"),
    )


@main.group(name="auth")
@click.pass_context
def auth(ctx: click.Context) -> None:
    """Manage stored API credentials (auth.json)."""

    ctx.ensure_object(dict)


def _mask(value: str) -> str:
    if len(value) > 9:
        return f"{value[:5]}…{value[-2:]}"
    return "…"


def _read_secret(from_stdin: bool, name: str) -> str:
    if from_stdin:
        stream = cast(io.TextIOBase, click.get_text_stream("stdin"))
        return stream.readline().strip()
    return getpass.getpass(f"{name}: ").strip()


@auth.command("set")
@click.argument("name")
@click.option("--stdin", "from_stdin", is_flag=True, help="Read the secret from stdin instead of a hidden prompt.")
@click.option(
    "--expires",
    "expires_at",
    default=None,
    metavar="ISO",
    help="Optional expiry timestamp, e.g. 2026-10-03.",
)
def auth_set(name: str, from_stdin: bool, expires_at: str | None) -> None:
    """Store one credential NAME (e.g. MINERU_API_KEY) in the user auth store.

    The secret value is read from a hidden prompt afterwards, or piped via
    --stdin. NAME is the lookup key referenced by `api_key_env` in your
    models.yaml, not the secret itself.
    """

    try:
        name = validate_credential_name(name)
    except ChemExError as exc:
        raise click.UsageError(str(exc)) from exc
    value = _read_secret(from_stdin, name)
    try:
        set_credential(name, value, expires_at=expires_at)
    except ChemExError as exc:
        raise click.UsageError(str(exc)) from exc
    click.echo(f"Stored {name} ({_mask(value)}) in {auth_store_path().as_posix()}")


@auth.command("list")
@click.option("--json", "as_json", is_flag=True, help="Print stored credentials as JSON.")
def auth_list(as_json: bool) -> None:
    """List stored credentials (masked)."""

    store = load_auth_store()
    rows: list[dict[str, object]] = []
    for name in sorted(store.credentials):
        entry = store.credentials[name]
        days = expires_in_days(entry)
        if days is not None and days < 0:
            status = "expired"
        elif days is not None and days < EXPIRING_SOON_DAYS:
            status = "expiring"
        else:
            status = "ok"
        rows.append(
            {
                "name": name,
                "masked": _mask(entry.value),
                "status": status,
                "updated_at": entry.updated_at,
                "expires_at": entry.expires_at,
                "expires_in_days": days,
            }
        )
    if as_json:
        click.echo(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        click.echo(f"No stored credentials in {auth_store_path().as_posix()}")
        return
    for row in rows:
        expiry = ""
        if row["expires_in_days"] is not None:
            expiry = f" | expires in {row['expires_in_days']}d"
        click.echo(f"{row['status']:8} {row['name']}: {row['masked']}{expiry}")


@auth.command("remove")
@click.argument("name")
def auth_remove(name: str) -> None:
    """Remove one credential NAME from the user auth store."""

    try:
        removed = remove_credential(name)
    except ChemExError as exc:
        raise click.UsageError(str(exc)) from exc
    if removed:
        click.echo(f"Removed {name}")
    else:
        click.echo(f"{name} was not stored")
        raise click.exceptions.Exit(1)


def _probe_credential(name: str, config: AppConfig) -> tuple[bool, str]:
    """Run a live probe for one credential; returns (ok, detail)."""

    value = credential_value(name, what="live probe")
    headers = {"Authorization": f"Bearer {value}"}
    specs = [spec for spec in (config.models.text, config.models.vision, config.models.reasoning) if spec is not None]
    if config.mineru.api_key_env == name:
        url = f"{config.mineru.base_url.rstrip('/')}/api/v4/file-urls/batch"
        response = httpx.post(
            url,
            headers=headers,
            json={
                "enable_formula": True,
                "language": config.mineru.language,
                "files": [{"name": "probe.pdf", "is_ocr": True}],
            },
            timeout=30,
        )
    else:
        spec = next((item for item in specs if item.api_key_env == name), None)
        if spec is None:
            known = ", ".join([config.mineru.api_key_env, *(item.api_key_env for item in specs)])
            raise click.UsageError(f"No live probe for credential {name!r}; known names: {known}")
        response = httpx.get(f"{spec.base_url.rstrip('/')}/models", headers=headers, timeout=30)
    return response.status_code == 200, f"HTTP {response.status_code}"


@auth.command("test")
@click.argument("name")
@click.pass_context
def auth_test(ctx: click.Context, name: str) -> None:
    """Live-probe one credential against its endpoint (exits 2 on failure)."""

    obj = _ctx_obj(ctx)
    config = load_config(
        obj.get("config_path"),
        profile=obj.get("profile"),
        models_path=obj.get("models_path"),
    )
    try:
        ok, detail = _probe_credential(name, config)
    except ChemExError as exc:
        raise click.UsageError(str(exc)) from exc
    if ok:
        click.echo(f"OK      {name}: {detail}")
        return
    click.echo(f"INVALID {name}: {detail}")
    raise click.exceptions.Exit(2)


def _ctx_obj(ctx: click.Context) -> CliContext:
    """Return the typed CLI context object."""

    return cast(CliContext, cast(object, ctx.ensure_object(dict)))


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
    click.echo(f"Output: {summary.run_dir}")
    if summary.status == "awaiting_input":
        click.echo(f"Awaiting tasks: {len(summary.awaiting)}")
        click.echo(f"Task file: {(Path(summary.run_dir) / 'tasks/extraction.jsonl').as_posix()}")


def _print_submit_summary(summary: SubmitSummary) -> None:
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


def _print_status(summary: RunSummary, as_json: bool) -> None:
    """Print the machine-readable run summary in human or JSON form."""

    if as_json:
        payload = summary.model_dump(mode="json")
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    click.echo(f"Run: {summary.run_id}")
    click.echo(f"Status: {summary.status}")
    for name, stage in summary.stages.items():
        click.echo(f"  {name}: {stage}")
    if summary.tasks:
        click.echo("Tasks:")
        for kind, data in summary.tasks.items():
            click.echo(
                f"  {kind}: awaiting={data.awaiting} fulfilled={data.fulfilled}"
            )
            for task_id in data.awaiting_task_ids[:5]:
                click.echo(f"    awaiting: {task_id}")


if __name__ == "__main__":
    main()
