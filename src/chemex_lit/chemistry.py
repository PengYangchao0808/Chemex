"""Deterministic chemistry: one validation pass and structure rendering."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Iterable, Literal

from chemex_lit.models import ReactionCandidate, StructureCandidate, ValidationIssue


@dataclass(frozen=True)
class ValidationOutcome:
    issues: list[ValidationIssue]
    canonical_smiles: dict[str, str]


class Validator:
    """Apply syntax, stereo, duplicate, collapse, and reaction completeness rules."""

    def validate(
        self,
        reactions: list[ReactionCandidate],
        structures: list[StructureCandidate],
    ) -> ValidationOutcome:
        issues: list[ValidationIssue] = []
        canonical: dict[str, str] = {}

        for reaction in reactions:
            if not reaction.reactants:
                issues.append(self._issue("R001_REACTANT_MISSING", "error", reaction.candidate_id, "Reaction has no reactant"))
            if not reaction.products:
                issues.append(self._issue("R002_PRODUCT_MISSING", "error", reaction.candidate_id, "Reaction has no product"))
            if not reaction.evidence_ids:
                issues.append(self._issue("R003_EVIDENCE_MISSING", "warning", reaction.candidate_id, "Reaction has no traceable evidence reference"))

        labels: dict[str, list[StructureCandidate]] = {}
        structures_by_smiles: dict[str, set[str]] = {}
        for structure in structures:
            parsed = self.canonicalize(structure.smiles)
            if parsed is None:
                issues.append(self._issue("V001_SMILES_PARSE_FAILED", "error", structure.candidate_id, f"Invalid SMILES: {structure.smiles}"))
                continue
            canonical[structure.candidate_id] = parsed
            label = normalize_label(structure.compound_label)
            if label:
                labels.setdefault(label, []).append(structure)
                structures_by_smiles.setdefault(parsed, set()).add(label)
            if self._has_unspecified_stereo(structure.smiles):
                issues.append(self._issue("V003_STEREO_UNSPECIFIED", "warning", structure.candidate_id, "Potential stereocenter is not fully specified"))

        for label, candidates in labels.items():
            values = {canonical.get(item.candidate_id) for item in candidates}
            values.discard(None)
            if len(values) > 1:
                for candidate in candidates:
                    issues.append(self._issue("V005_LABEL_STRUCTURE_CONFLICT", "error", candidate.candidate_id, f"Label {label} maps to multiple structures"))

        for smiles, compound_labels in structures_by_smiles.items():
            if len(compound_labels) > 1:
                target = next(
                    item.candidate_id
                    for item in structures
                    if canonical.get(item.candidate_id) == smiles
                )
                issues.append(self._issue("V007_STRUCTURE_COLLAPSE", "warning", target, f"One structure maps to labels {sorted(compound_labels)}"))

        return ValidationOutcome(issues=unique_issues(issues), canonical_smiles=canonical)

    @staticmethod
    def canonicalize(smiles: str | None) -> str | None:
        if not smiles:
            return None
        try:
            from rdkit import Chem

            molecule = Chem.MolFromSmiles(smiles)
            if molecule is None:
                return None
            return Chem.MolToSmiles(molecule, canonical=True, isomericSmiles=True)
        except (ImportError, ValueError, TypeError):
            return None

    @staticmethod
    def _has_unspecified_stereo(smiles: str) -> bool:
        try:
            from rdkit import Chem

            molecule = Chem.MolFromSmiles(smiles)
            if molecule is None:
                return False
            centers = Chem.FindMolChiralCenters(molecule, includeUnassigned=True)
            return any(assignment == "?" for _, assignment in centers)
        except (ImportError, ValueError, TypeError):
            return False

    @staticmethod
    def _issue(
        code: str,
        severity: Literal["info", "warning", "error"],
        target_id: str,
        message: str,
    ) -> ValidationIssue:
        return ValidationIssue(code=code, severity=severity, target_id=target_id, message=message)


def normalize_label(label: str | None) -> str:
    if not label:
        return ""
    return re.sub(r"\s+", "", label).lower()


def unique_issues(issues: Iterable[ValidationIssue]) -> list[ValidationIssue]:
    """Deduplicate validation issues by ``(code, target_id, message)``, preserving order."""

    unique: dict[tuple[str, str, str], ValidationIssue] = {}
    for issue in issues:
        unique[(issue.code, issue.target_id, issue.message)] = issue
    return list(unique.values())


def render_smiles(smiles: str, size: tuple[int, int] = (360, 240)) -> bytes | None:
    """Render one SMILES to PNG bytes, or ``None`` when RDKit cannot parse it."""

    try:
        from rdkit import Chem
        from rdkit.Chem import Draw  # pyright: ignore[reportAttributeAccessIssue]

        molecule = Chem.MolFromSmiles(smiles)
        if molecule is None:
            return None
        image = Draw.MolToImage(molecule, size=size)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except (ImportError, ValueError, OSError):
        return None
