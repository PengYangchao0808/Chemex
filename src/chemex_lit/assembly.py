"""Deterministic assembly of candidates into stable reaction records."""

from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Iterable

from chemex_lit.chemistry.validate import ValidationOutcome, Validator, normalize_label
from chemex_lit.extraction import stable_id
from chemex_lit.models import (
    CompoundRef,
    ReactionCandidate,
    ReactionRecord,
    StructureCandidate,
    ValidationIssue,
)


class Assembler:
    """Merge source agreement and structure resolution without generative edits."""

    def assemble(
        self,
        reactions: list[ReactionCandidate],
        structures: list[StructureCandidate],
        validation: ValidationOutcome,
    ) -> list[ReactionRecord]:
        structure_index = self._structure_index(structures, validation)
        issue_index = _issue_index(validation.issues)
        groups: dict[tuple[tuple[str, ...], tuple[str, ...]], list[ReactionCandidate]] = defaultdict(list)
        for candidate in reactions:
            groups[self._signature(candidate)].append(candidate)

        records: list[ReactionRecord] = []
        for signature, candidates in groups.items():
            records.append(
                self._assemble_group(
                    signature,
                    candidates,
                    structure_index,
                    issue_index,
                )
            )
        return sorted(records, key=lambda item: item.reaction_id)

    def _assemble_group(
        self,
        signature: tuple[tuple[str, ...], tuple[str, ...]],
        candidates: list[ReactionCandidate],
        structures: dict[str, list[tuple[StructureCandidate, str]]],
        issue_index: dict[str, list[ValidationIssue]],
    ) -> ReactionRecord:
        base = max(candidates, key=lambda item: item.confidence)
        issues = [issue for candidate in candidates for issue in issue_index.get(candidate.candidate_id, [])]
        reactants, reactant_issues, reactant_resolved = self._resolve_compounds(base.reactants, structures, issue_index)
        products, product_issues, product_resolved = self._resolve_compounds(base.products, structures, issue_index)
        issues.extend(reactant_issues + product_issues)

        sources = {candidate.source for candidate in candidates}
        evidence = _ordered_unique(
            evidence_id for candidate in candidates for evidence_id in candidate.evidence_ids
        )
        field_issues = self._field_conflicts(candidates)
        issues.extend(field_issues)

        total_compounds = len(reactants) + len(products)
        resolved_ratio = (
            (reactant_resolved + product_resolved) / total_compounds if total_compounds else 0
        )
        error_count = sum(issue.severity == "error" for issue in issues)
        validation_quality = max(0.0, 1.0 - error_count / max(1, total_compounds + 1))
        confidence = min(
            1.0,
            0.35 * mean(item.confidence for item in candidates)
            + 0.25 * resolved_ratio
            + 0.20 * (1.0 if len(sources) > 1 else 0.5)
            + 0.20 * validation_quality,
        )

        status = "accepted"
        if error_count or confidence < 0.75 or resolved_ratio < 1:
            status = "needs_review"

        reaction_id = stable_id(
            "reaction",
            {"signature": signature, "evidence": evidence},
        )
        return ReactionRecord(
            reaction_id=reaction_id,
            reactants=reactants,
            products=products,
            reagents=_ordered_unique(item for row in candidates for item in row.reagents),
            solvents=_ordered_unique(item for row in candidates for item in row.solvents),
            temperature_c=base.temperature_c,
            time=base.time,
            yield_pct=base.yield_pct,
            evidence_ids=evidence,
            confidence=round(confidence, 4),
            review_status=status,
            issues=_unique_issues(issues),
        )

    def _resolve_compounds(
        self,
        compounds: list[CompoundRef],
        structures: dict[str, list[tuple[StructureCandidate, str]]],
        issue_index: dict[str, list[ValidationIssue]],
    ) -> tuple[list[CompoundRef], list[ValidationIssue], int]:
        resolved: list[CompoundRef] = []
        issues: list[ValidationIssue] = []
        resolved_count = 0
        for compound in compounds:
            label = normalize_label(compound.label)
            candidates = structures.get(label, []) if label else []
            values = {canonical for _, canonical in candidates}
            smiles = Validator.canonicalize(compound.smiles)
            if smiles:
                resolved_count += 1
            elif len(values) == 1:
                smiles = next(iter(values))
                resolved_count += 1
            elif len(values) > 1:
                issues.append(
                    ValidationIssue(
                        code="A001_STRUCTURE_AMBIGUOUS",
                        severity="error",
                        target_id=compound.label or "unlabelled",
                        message=f"Multiple structures are available for {compound.label}",
                    )
                )
            else:
                issues.append(
                    ValidationIssue(
                        code="A002_STRUCTURE_MISSING",
                        severity="warning",
                        target_id=compound.label or "unlabelled",
                        message=f"No structure was resolved for {compound.label or compound.name or 'compound'}",
                    )
                )
            for candidate, _ in candidates:
                issues.extend(issue_index.get(candidate.candidate_id, []))
            resolved.append(compound.model_copy(update={"smiles": smiles}))
        return resolved, issues, resolved_count

    @staticmethod
    def _structure_index(
        structures: list[StructureCandidate],
        validation: ValidationOutcome,
    ) -> dict[str, list[tuple[StructureCandidate, str]]]:
        result: dict[str, list[tuple[StructureCandidate, str]]] = defaultdict(list)
        for candidate in structures:
            label = normalize_label(candidate.compound_label)
            canonical = validation.canonical_smiles.get(candidate.candidate_id)
            if label and canonical:
                result[label].append((candidate, canonical))
        return result

    @staticmethod
    def _signature(candidate: ReactionCandidate) -> tuple[tuple[str, ...], tuple[str, ...]]:
        def keys(compounds: list[CompoundRef]) -> tuple[str, ...]:
            values = [normalize_label(item.label) or (item.name or "").strip().lower() for item in compounds]
            return tuple(sorted(value for value in values if value))

        signature = (keys(candidate.reactants), keys(candidate.products))
        if not signature[0] and not signature[1]:
            return ((candidate.candidate_id,), ())
        return signature

    @staticmethod
    def _field_conflicts(candidates: list[ReactionCandidate]) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        fields = ("temperature_c", "time", "yield_pct")
        for field in fields:
            values = {getattr(candidate, field) for candidate in candidates if getattr(candidate, field) is not None}
            if len(values) > 1:
                issues.append(
                    ValidationIssue(
                        code="A003_FIELD_CONFLICT",
                        severity="warning",
                        target_id=candidates[0].candidate_id,
                        message=f"Conflicting {field} values: {sorted(str(value) for value in values)}",
                    )
                )
        return issues


def _issue_index(issues: Iterable[ValidationIssue]) -> dict[str, list[ValidationIssue]]:
    result: dict[str, list[ValidationIssue]] = defaultdict(list)
    for issue in issues:
        result[issue.target_id].append(issue)
    return result


def _ordered_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _unique_issues(issues: Iterable[ValidationIssue]) -> list[ValidationIssue]:
    result: dict[tuple[str, str, str], ValidationIssue] = {}
    for issue in issues:
        result[(issue.code, issue.target_id, issue.message)] = issue
    return list(result.values())
