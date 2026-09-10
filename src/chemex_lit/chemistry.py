"""Deterministic chemistry: one validation pass and structure rendering."""

from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Literal

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


# ---------------------------------------------------------------------------
# Stereo analysis and structure comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StereoCenter:
    """One tetrahedral stereocenter detected by RDKit."""

    atom_index: int
    assignment: str | None
    specified: bool


@dataclass(frozen=True)
class StereoBond:
    """One double bond with potential E/Z stereochemistry."""

    bond_index: int
    begin_atom: int
    end_atom: int
    assignment: str | None
    specified: bool


@dataclass(frozen=True)
class StereoAnalysis:
    """Result of stereochemical analysis of one SMILES string."""

    canonical_smiles: str
    centers: list[StereoCenter]
    bonds: list[StereoBond]
    rdkit_available: bool


@dataclass(frozen=True)
class StructureComparison:
    """Layered comparison result between two SMILES structures."""

    level: str
    detail: str
    comparable: bool


def analyze_stereo(smiles: str) -> StereoAnalysis | None:
    """Analyze stereochemistry of a SMILES string.

    Returns ``StereoAnalysis`` with detected stereocenters and stereo bonds,
    or ``None`` if the SMILES cannot be parsed.  Atom indices are RDKit atom
    indices of the parsed molecule and are valid only with the returned
    ``canonical_smiles``.

    Args:
        smiles: A SMILES string to analyze.

    Returns:
        StereoAnalysis or None if the SMILES is unparseable.
    """
    try:
        from rdkit import Chem
    except (ImportError, ValueError, TypeError):
        return StereoAnalysis(
            canonical_smiles=smiles,
            centers=[],
            bonds=[],
            rdkit_available=False,
        )

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None

    canonical = Chem.MolToSmiles(mol, canonical=True, isomericSmiles=True)

    centers: list[StereoCenter] = []
    for atom_idx, assignment in Chem.FindMolChiralCenters(
        mol, includeUnassigned=True
    ):
        specified = assignment != "?"
        centers.append(StereoCenter(
            atom_index=atom_idx,
            assignment=assignment if specified else None,
            specified=specified,
        ))

    bonds: list[StereoBond] = []
    for bond in mol.GetBonds():
        if bond.GetBondType() != Chem.rdchem.BondType.DOUBLE:
            continue
        stereo = bond.GetStereo()
        is_specified = stereo in (
            Chem.rdchem.BondStereo.STEREOE,
            Chem.rdchem.BondStereo.STEREOZ,
        )
        assignment = None
        if is_specified:
            assignment = (
                "E" if stereo == Chem.rdchem.BondStereo.STEREOE else "Z"
            )
        bonds.append(StereoBond(
            bond_index=bond.GetIdx(),
            begin_atom=bond.GetBeginAtomIdx(),
            end_atom=bond.GetEndAtomIdx(),
            assignment=assignment,
            specified=is_specified,
        ))

    return StereoAnalysis(
        canonical_smiles=canonical,
        centers=centers,
        bonds=bonds,
        rdkit_available=True,
    )


def compare_structures(
    smiles_a: str | None,
    smiles_b: str | None,
    *,
    normalization: Literal["none", "strip_charge", "strip_isotopes"] = "none",
) -> StructureComparison:
    """Layered, deterministic structure comparison.

    Compares two SMILES at connectivity, stereo, and charge/salt/isotope
    levels.  Uses substructure matching for atom correspondence — never
    infers stereo from ``@``/``@@`` string differences.

    Args:
        smiles_a: First SMILES string, or None.
        smiles_b: Second SMILES string, or None.
        normalization: Controls whether charge or isotope differences are
            collapsed.  ``strip_charge`` makes molecules differing only in
            formal charge report as identical; ``strip_isotopes`` does the
            same for isotope labels.

    Returns:
        StructureComparison with level, detail, and comparable flag.
    """
    a_empty = not smiles_a or not smiles_a.strip()
    b_empty = not smiles_b or not smiles_b.strip()

    if a_empty and b_empty:
        return StructureComparison(
            level="uncomparable",
            detail="Both inputs are None or empty",
            comparable=False,
        )
    if a_empty or b_empty:
        return StructureComparison(
            level="missing_one_side",
            detail=f"Only {'B' if a_empty else 'A'} side provided",
            comparable=False,
        )

    try:
        from rdkit import Chem
    except (ImportError, ValueError, TypeError):
        return StructureComparison(
            level="uncomparable",
            detail="RDKit not available",
            comparable=False,
        )

    # Both inputs are non-None non-empty str after the guards above.
    assert smiles_a is not None and smiles_b is not None  # noqa: S101
    mol_a = Chem.MolFromSmiles(smiles_a)
    mol_b = Chem.MolFromSmiles(smiles_b)

    if mol_a is None or mol_b is None:
        parts: list[str] = []
        if mol_a is None:
            parts.append(f"Cannot parse A: {smiles_a}")
        if mol_b is None:
            parts.append(f"Cannot parse B: {smiles_b}")
        return StructureComparison(
            level="uncomparable",
            detail="; ".join(parts),
            comparable=False,
        )

    # Layer 1: isomeric canonical SMILES
    iso_a = Chem.MolToSmiles(mol_a, canonical=True, isomericSmiles=True)
    iso_b = Chem.MolToSmiles(mol_b, canonical=True, isomericSmiles=True)
    if iso_a == iso_b:
        return StructureComparison(
            level="identical",
            detail="Canonical isomeric SMILES are equal",
            comparable=True,
        )

    # Layer 2: non-isomeric canonical SMILES (connectivity only)
    niso_a = Chem.MolToSmiles(mol_a, canonical=True, isomericSmiles=False)
    niso_b = Chem.MolToSmiles(mol_b, canonical=True, isomericSmiles=False)
    if niso_a == niso_b:
        level = _classify_stereo_diff(mol_a, mol_b)
        return StructureComparison(
            level=level,
            detail=f"Same connectivity ({niso_a}), stereo differs",
            comparable=True,
        )

    # Layer 3: normalization-aware identity check
    if normalization == "strip_charge":
        norm_a = _strip_charges_canonical(mol_a)
        norm_b = _strip_charges_canonical(mol_b)
        if norm_a is not None and norm_b is not None and norm_a == norm_b:
            return StructureComparison(
                level="identical",
                detail="Identical after stripping formal charges",
                comparable=True,
            )
    elif normalization == "strip_isotopes":
        norm_a = _strip_isotopes_canonical(mol_a)
        norm_b = _strip_isotopes_canonical(mol_b)
        if norm_a is not None and norm_b is not None and norm_a == norm_b:
            return StructureComparison(
                level="identical",
                detail="Identical after stripping isotope labels",
                comparable=True,
            )

    # Layer 4: charge/salt/isotope skeleton comparison
    skel_a = _neutral_skeleton_smiles(mol_a)
    skel_b = _neutral_skeleton_smiles(mol_b)
    if skel_a is not None and skel_b is not None and skel_a == skel_b:
        return StructureComparison(
            level="charge_salt_isotope_differs",
            detail=f"Same skeleton ({skel_a}), charge/salt/isotope differs",
            comparable=True,
        )

    # Layer 5: different connectivity
    return StructureComparison(
        level="connectivity_differs",
        detail=f"Different connectivity: A={niso_a}, B={niso_b}",
        comparable=True,
    )


def render_smiles_svg(
    smiles: str,
    size: tuple[int, int] = (360, 240),
    *,
    highlight_atoms: list[int] | None = None,
) -> str | None:
    """Render one SMILES to SVG string, or ``None`` on failure.

    Args:
        smiles: A SMILES string to render.
        size: Width and height in pixels.
        highlight_atoms: Optional atom indices to highlight.

    Returns:
        SVG markup string, or None if RDKit cannot parse the SMILES.
    """
    try:
        from rdkit import Chem
        from rdkit.Chem import Draw  # pyright: ignore[reportAttributeAccessIssue]
        from rdkit.Chem import rdDepictor

        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        rdDepictor.Compute2DCoords(mol)
        drawer = Draw.MolDraw2DSVG(size[0], size[1])
        draw_kwargs: dict[str, Any] = {}
        if highlight_atoms is not None:
            draw_kwargs["highlightAtoms"] = highlight_atoms
        drawer.DrawMolecule(mol, **draw_kwargs)
        drawer.FinishDrawing()
        return drawer.GetDrawingText()
    except (ImportError, ValueError, OSError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Private helpers for compare_structures
# ---------------------------------------------------------------------------


def _classify_stereo_diff(mol_a: Any, mol_b: Any) -> str:
    """Classify stereo difference between molecules with same connectivity.

    Uses ``GetSubstructMatch`` for atom correspondence.  Never infers
    stereo from ``@``/``@@`` string differences.
    """
    from rdkit import Chem
    from rdkit.Chem import rdchem

    match = mol_b.GetSubstructMatch(mol_a)
    if len(match) != mol_a.GetNumAtoms():
        return "stereo_only"

    centers_a = dict(Chem.FindMolChiralCenters(mol_a, includeUnassigned=True))
    centers_b = dict(Chem.FindMolChiralCenters(mol_b, includeUnassigned=True))

    has_spec_diff = False
    has_value_diff = False

    for idx_a, assign_a in centers_a.items():
        idx_b = match[idx_a]
        assign_b = centers_b.get(idx_b, "?")
        if assign_a == "?" and assign_b != "?":
            has_spec_diff = True
        elif assign_a != "?" and assign_b == "?":
            has_spec_diff = True
        elif assign_a != assign_b and assign_a != "?" and assign_b != "?":
            has_value_diff = True

    for bond_a in mol_a.GetBonds():
        if bond_a.GetBondType() != rdchem.BondType.DOUBLE:
            continue
        begin_b = match[bond_a.GetBeginAtomIdx()]
        end_b = match[bond_a.GetEndAtomIdx()]
        bond_b = mol_b.GetBondBetweenAtoms(begin_b, end_b)
        if bond_b is None or bond_b.GetBondType() != rdchem.BondType.DOUBLE:
            continue
        stereo_a = bond_a.GetStereo()
        stereo_b = bond_b.GetStereo()
        a_spec = stereo_a in (
            rdchem.BondStereo.STEREOE, rdchem.BondStereo.STEREOZ
        )
        b_spec = stereo_b in (
            rdchem.BondStereo.STEREOE, rdchem.BondStereo.STEREOZ
        )
        if a_spec and not b_spec:
            has_spec_diff = True
        elif not a_spec and b_spec:
            has_spec_diff = True
        elif a_spec and b_spec and stereo_a != stereo_b:
            has_value_diff = True

    if has_value_diff:
        return "stereo_only"
    if has_spec_diff:
        return "stereo_specificity_differs"
    return "stereo_only"


def _clean_bracket(content: str) -> str:
    """Strip isotope digits and charge from one SMILES bracket body."""
    c = re.sub(r"^\d+", "", content)
    c = re.sub(r"[+-]\d*$", "", c)
    return c


def _strip_annotations(smiles: str, *, charges: bool, isotopes: bool) -> str:
    """Strip charge and/or isotope annotations from a canonical SMILES string.

    Operates on the SMILES string level to avoid RDKit bond-perception
    changes that occur when formal charges are modified on the molecule.
    """

    def _replace(match: re.Match[str]) -> str:
        body = match.group(1)
        if isotopes:
            body = re.sub(r"^\d+", "", body)
        if charges:
            body = re.sub(r"[+-]\d*$", "", body)
        return body

    return re.sub(r"\[([^\]]+)\]", _replace, smiles)


def _neutral_skeleton_smiles(mol: Any) -> str | None:
    """Canonical non-iso SMILES of largest fragment, charges/isotopes stripped."""
    from rdkit import Chem

    try:
        smiles = Chem.MolToSmiles(mol, canonical=True, isomericSmiles=False)
        stripped = _strip_annotations(smiles, charges=True, isotopes=True)
        frags = stripped.split(".")
        if not frags:
            return None
        return max(frags, key=len)
    except Exception:
        return None


def _strip_charges_canonical(mol: Any) -> str | None:
    """Canonical non-isomeric SMILES with formal charges stripped."""
    from rdkit import Chem

    try:
        smiles = Chem.MolToSmiles(mol, canonical=True, isomericSmiles=False)
        return _strip_annotations(smiles, charges=True, isotopes=False)
    except Exception:
        return None


def _strip_isotopes_canonical(mol: Any) -> str | None:
    """Canonical non-isomeric SMILES with isotope labels stripped."""
    from rdkit import Chem

    try:
        smiles = Chem.MolToSmiles(mol, canonical=True, isomericSmiles=False)
        return _strip_annotations(smiles, charges=False, isotopes=True)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Structure rendering for the review workbench
# ---------------------------------------------------------------------------

RenderStatus = Literal["ok", "rdkit_missing", "invalid_smiles", "missing_smiles"]
RenderMode = Literal["normal", "stereo", "atommap"]


@dataclass(frozen=True)
class StructureRender:
    """Outcome of one structure render attempt."""

    status: RenderStatus
    png: bytes | None = None
    svg: str | None = None


def render_structure(
    smiles: str | None,
    *,
    size: tuple[int, int] = (420, 280),
    mode: RenderMode = "normal",
    highlight_atoms: list[int] | None = None,
) -> StructureRender:
    """Render one SMILES to both PNG (2x retina) and SVG in a single call.

    Args:
        smiles: A SMILES string, or None/blank for missing.
        size: Base width and height in pixels.  PNG is rendered at 2x this
            size for retina sharpness; SVG uses the given size directly.
        mode: Rendering mode — ``"normal"`` for plain depiction,
            ``"stereo"`` for R/S/E/Z annotations with unassigned-stereo
            highlights, or ``"atommap"`` for atom-index labels.
        highlight_atoms: Extra atom indices to highlight in any mode (union
            with mode-computed highlights).

    Returns:
        StructureRender with status and optional PNG/SVG payloads.
    """
    if not smiles or not smiles.strip():
        return StructureRender(status="missing_smiles")

    try:
        from rdkit import Chem
        from rdkit.Chem import Draw  # pyright: ignore[reportAttributeAccessIssue]
        from rdkit.Chem import rdDepictor
    except (ImportError, ValueError, TypeError):
        return StructureRender(status="rdkit_missing")

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return StructureRender(status="invalid_smiles")

    rdDepictor.Compute2DCoords(mol)

    # Collect mode-specific highlights.
    mode_atoms = _stereo_highlight_atoms(mol, mode)
    all_highlights: list[int] | None = None
    if mode_atoms or highlight_atoms:
        merged: set[int] = set()
        if mode_atoms:
            merged.update(mode_atoms)
        if highlight_atoms:
            merged.update(highlight_atoms)
        all_highlights = sorted(merged)

    # --- PNG at 2x for retina ---
    png_w, png_h = size[0] * 2, size[1] * 2
    png_drawer = Draw.MolDraw2DCairo(png_w, png_h)
    _apply_mode_options(png_drawer, mode)
    png_kwargs: dict[str, Any] = {}
    if all_highlights is not None:
        png_kwargs["highlightAtoms"] = all_highlights
    png_drawer.DrawMolecule(mol, **png_kwargs)
    png_drawer.FinishDrawing()
    png_bytes = png_drawer.GetDrawingText()

    # --- SVG at given size ---
    svg_drawer = Draw.MolDraw2DSVG(size[0], size[1])
    _apply_mode_options(svg_drawer, mode)
    svg_kwargs: dict[str, Any] = {}
    if all_highlights is not None:
        svg_kwargs["highlightAtoms"] = all_highlights
    svg_drawer.DrawMolecule(mol, **svg_kwargs)
    svg_drawer.FinishDrawing()
    svg_text = svg_drawer.GetDrawingText()

    return StructureRender(status="ok", png=png_bytes, svg=svg_text)


def _stereo_highlight_atoms(mol: Any, mode: RenderMode) -> list[int]:
    """Compute atom indices to highlight for stereo mode."""
    if mode != "stereo":
        return []
    from rdkit import Chem

    atoms: set[int] = set()
    for atom_idx, assignment in Chem.FindMolChiralCenters(
        mol, includeUnassigned=True
    ):
        if assignment == "?":
            atoms.add(atom_idx)

    try:
        Chem.FindPotentialStereoBonds(mol)
        for bond in mol.GetBonds():
            if bond.GetStereo() == Chem.rdchem.BondStereo.STEREOANY:
                atoms.add(bond.GetBeginAtomIdx())
                atoms.add(bond.GetEndAtomIdx())
    except (AttributeError, TypeError):
        pass

    return sorted(atoms)


def _apply_mode_options(drawer: Any, mode: RenderMode) -> None:
    """Set drawer options for the given rendering mode."""
    if mode == "stereo":
        drawer.drawOptions().addStereoAnnotation = True
    elif mode == "atommap":
        drawer.drawOptions().addAtomIndices = True


# ---------------------------------------------------------------------------
# Hash-keyed asset helpers for the review workbench
# ---------------------------------------------------------------------------


def structure_hash(smiles: str) -> str | None:
    """Return a short hex hash of the canonical isomeric SMILES.

    Args:
        smiles: A SMILES string.

    Returns:
        First 16 hex chars of sha256 of canonical SMILES, or None if
        the SMILES cannot be canonicalized.
    """
    canonical = Validator.canonicalize(smiles)
    if canonical is None:
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def drawing_config_hash(mode: str, size: tuple[int, int]) -> str:
    """Return a short hex hash of the drawing configuration.

    Args:
        mode: Rendering mode name.
        size: Base width and height in pixels.

    Returns:
        First 8 hex chars of sha256 of a JSON object containing mode,
        size, and the RDKit version string.
    """
    try:
        from rdkit import rdBase

        rdkit_version: str = rdBase.rdkitVersion
    except (ImportError, ValueError, TypeError, AttributeError):
        rdkit_version = "<none>"

    payload = json.dumps(
        {"mode": mode, "size": list(size), "rdkit": rdkit_version},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def asset_basename(
    smiles: str, mode: str, size: tuple[int, int] = (420, 280)
) -> str | None:
    """Return a filesystem-safe basename for a rendered structure asset.

    Args:
        smiles: A SMILES string.
        mode: Rendering mode name.
        size: Base width and height in pixels.

    Returns:
        ``"{structure_hash}-{config_hash}"`` (hex + dash only), or None
        when the SMILES cannot be canonicalized.
    """
    s_hash = structure_hash(smiles)
    if s_hash is None:
        return None
    c_hash = drawing_config_hash(mode, size)
    return f"{s_hash}-{c_hash}"


# ---------------------------------------------------------------------------
# Gold-comparison highlight atoms
# ---------------------------------------------------------------------------


def gold_highlight_atoms(
    extracted_smiles: str, gold_smiles: str
) -> tuple[list[int], list[int]] | None:
    """Find atom indices that differ between extracted and gold structures.

    Returns a pair of index lists ``(extracted_atom_indices, gold_atom_indices)``
    suitable for highlighting in a side-by-side render.  Returns ``None``
    when the pair cannot be compared or the mapping is ambiguous.

    Args:
        extracted_smiles: SMILES from the extraction pipeline.
        gold_smiles: Gold-standard SMILES.

    Returns:
        Tuple of two lists of atom indices, or None.
    """
    try:
        from rdkit import Chem
        from rdkit.Chem import rdFMCS  # pyright: ignore[reportAttributeAccessIssue]
    except (ImportError, ValueError, TypeError):
        return None

    mol_a = Chem.MolFromSmiles(extracted_smiles)
    mol_b = Chem.MolFromSmiles(gold_smiles)
    if mol_a is None or mol_b is None:
        return None

    iso_a = Chem.MolToSmiles(mol_a, canonical=True, isomericSmiles=True)
    iso_b = Chem.MolToSmiles(mol_b, canonical=True, isomericSmiles=True)
    if iso_a == iso_b:
        return ([], [])

    # Try unambiguous full-structure substructure match in both directions.
    matches_a_in_b = mol_b.GetSubstructMatches(mol_a)
    matches_b_in_a = mol_a.GetSubstructMatches(mol_b)

    mapping: dict[int, int] | None = None

    if len(matches_a_in_b) == 1 and len(matches_a_in_b[0]) == mol_a.GetNumAtoms():
        # a fully matches b — map a→b
        mapping = dict(enumerate(matches_a_in_b[0]))
    elif len(matches_b_in_a) == 1 and len(matches_b_in_a[0]) == mol_b.GetNumAtoms():
        # b fully matches a — map b→a, then invert
        raw = matches_b_in_a[0]
        mapping = {v: k for k, v in enumerate(raw)}

    if mapping is None:
        # Fall back to MCS.
        mcs_result = rdFMCS.FindMCS(
            [mol_a, mol_b],
            matchValences=True,
            ringMatchesRingOnly=False,
            completeRingsOnly=False,
            timeout=5,
        )
        mcs_smarts = mcs_result.smartsString
        if not mcs_smarts:
            return None
        mcs_mol = Chem.MolFromSmarts(mcs_smarts)
        if mcs_mol is None:
            return None
        if mcs_mol.GetNumAtoms() < 3:
            return None

        mcs_in_a = mol_a.GetSubstructMatches(mcs_mol)
        mcs_in_b = mol_b.GetSubstructMatches(mcs_mol)
        if len(mcs_in_a) != 1 or len(mcs_in_b) != 1:
            return None

        mcs_a_indices = set(mcs_in_a[0])
        mcs_b_indices = set(mcs_in_b[0])

        # Build mapping from the MCS alignment: position i in MCS →
        # a_indices[i] ↔ b_indices[i].
        mcs_a_list = mcs_in_a[0]
        mcs_b_list = mcs_in_b[0]
        mapping = {mcs_a_list[i]: mcs_b_list[i] for i in range(len(mcs_a_list))}

        diff_a = sorted(set(range(mol_a.GetNumAtoms())) - mcs_a_indices)
        diff_b = sorted(set(range(mol_b.GetNumAtoms())) - mcs_b_indices)
        if diff_a or diff_b:
            return (diff_a, diff_b)

        # All atoms in MCS — no connectivity difference, check stereo.
        return _stereo_diff_atoms_from_mapping(mol_a, mol_b, mapping)

    # We have a full mapping. Check stereo differences.
    return _stereo_diff_atoms_from_mapping(mol_a, mol_b, mapping)


def _stereo_diff_atoms_from_mapping(
    mol_a: Any, mol_b: Any, mapping: dict[int, int]
) -> tuple[list[int], list[int]] | None:
    """Given an atom mapping, find atoms with stereo differences."""
    from rdkit import Chem
    from rdkit.Chem import rdchem

    centers_a = dict(Chem.FindMolChiralCenters(mol_a, includeUnassigned=True))
    centers_b = dict(Chem.FindMolChiralCenters(mol_b, includeUnassigned=True))

    diff_a: list[int] = []
    diff_b: list[int] = []

    for idx_a, assign_a in centers_a.items():
        idx_b = mapping.get(idx_a)
        if idx_b is None:
            continue
        assign_b = centers_b.get(idx_b, "?")
        if assign_a != assign_b:
            diff_a.append(idx_a)
            diff_b.append(idx_b)

    for bond_a in mol_a.GetBonds():
        if bond_a.GetBondType() != rdchem.BondType.DOUBLE:
            continue
        begin_b = mapping.get(bond_a.GetBeginAtomIdx())
        end_b = mapping.get(bond_a.GetEndAtomIdx())
        if begin_b is None or end_b is None:
            continue
        bond_b = mol_b.GetBondBetweenAtoms(begin_b, end_b)
        if bond_b is None or bond_b.GetBondType() != rdchem.BondType.DOUBLE:
            continue
        stereo_a = bond_a.GetStereo()
        stereo_b = bond_b.GetStereo()
        a_spec = stereo_a in (
            rdchem.BondStereo.STEREOE, rdchem.BondStereo.STEREOZ
        )
        b_spec = stereo_b in (
            rdchem.BondStereo.STEREOE, rdchem.BondStereo.STEREOZ
        )
        if a_spec != b_spec or (a_spec and b_spec and stereo_a != stereo_b):
            diff_a.extend([bond_a.GetBeginAtomIdx(), bond_a.GetEndAtomIdx()])
            diff_b.extend([begin_b, end_b])

    if not diff_a and not diff_b:
        return ([], [])
    return (sorted(set(diff_a)), sorted(set(diff_b)))
