"""Tests for stereo analysis, structure comparison, and SVG rendering."""

from __future__ import annotations

import sys

import pytest

from chemex_lit.chemistry import (
    StereoAnalysis,
    StereoBond,
    StereoCenter,
    StructureComparison,
    analyze_stereo,
    compare_structures,
    render_smiles_svg,
)


# --- analyze_stereo ---


def test_analyze_stereo_r_center() -> None:
    result = analyze_stereo("C[C@H](O)F")
    assert result is not None
    assert result.rdkit_available is True
    assert len(result.centers) == 1
    center = result.centers[0]
    assert center.atom_index == 1
    assert center.assignment == "R"
    assert center.specified is True


def test_analyze_stereo_s_center() -> None:
    result = analyze_stereo("C[C@@H](O)F")
    assert result is not None
    assert len(result.centers) == 1
    center = result.centers[0]
    assert center.assignment == "S"
    assert center.specified is True


def test_analyze_stereo_unspecified_center() -> None:
    result = analyze_stereo("CC(O)F")
    assert result is not None
    assert len(result.centers) == 1
    center = result.centers[0]
    assert center.assignment is None
    assert center.specified is False


def test_analyze_stereo_e_bond() -> None:
    result = analyze_stereo("C/C=C/C")
    assert result is not None
    assert len(result.bonds) == 1
    bond = result.bonds[0]
    assert bond.assignment == "E"
    assert bond.specified is True


def test_analyze_stereo_z_bond() -> None:
    result = analyze_stereo("C/C=C\\C")
    assert result is not None
    assert len(result.bonds) == 1
    bond = result.bonds[0]
    assert bond.assignment == "Z"
    assert bond.specified is True


def test_analyze_stereo_unspecified_bond() -> None:
    result = analyze_stereo("CC=CC")
    assert result is not None
    assert len(result.bonds) == 1
    bond = result.bonds[0]
    assert bond.assignment is None
    assert bond.specified is False


def test_analyze_stereo_no_stereo() -> None:
    result = analyze_stereo("CCO")
    assert result is not None
    assert result.centers == []
    assert result.bonds == []
    assert result.rdkit_available is True


def test_analyze_stereo_invalid_smiles() -> None:
    assert analyze_stereo("not-smiles") is None


def test_analyze_stereo_canonical_smiles() -> None:
    result = analyze_stereo("OCC")
    assert result is not None
    assert result.canonical_smiles == "CCO"


def test_analyze_stereo_no_rdkit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "rdkit", None)
    result = analyze_stereo("CCO")
    assert result is not None
    assert result.rdkit_available is False
    assert result.centers == []
    assert result.bonds == []
    assert result.canonical_smiles == "CCO"


# --- compare_structures ---


def test_compare_identical() -> None:
    result = compare_structures("CCO", "CCO")
    assert result.level == "identical"
    assert result.comparable is True


def test_compare_identical_canonical() -> None:
    result = compare_structures("OCC", "CCO")
    assert result.level == "identical"
    assert result.comparable is True


def test_compare_stereo_only_chiral() -> None:
    result = compare_structures("C[C@H](O)F", "C[C@@H](O)F")
    assert result.level == "stereo_only"
    assert result.comparable is True


def test_compare_stereo_only_ez() -> None:
    result = compare_structures("C/C=C/C", "C/C=C\\C")
    assert result.level == "stereo_only"
    assert result.comparable is True


def test_compare_stereo_specificity_differs_chiral() -> None:
    result = compare_structures("C[C@H](O)F", "CC(O)F")
    assert result.level == "stereo_specificity_differs"
    assert result.comparable is True


def test_compare_stereo_specificity_differs_ez() -> None:
    result = compare_structures("C/C=C/C", "CC=CC")
    assert result.level == "stereo_specificity_differs"
    assert result.comparable is True


def test_compare_connectivity_differs() -> None:
    result = compare_structures("CCO", "CCC")
    assert result.level == "connectivity_differs"
    assert result.comparable is True


def test_compare_charge_salt_isotope_differs() -> None:
    result = compare_structures("CC(=O)O", "CC(=O)[O-]")
    assert result.level == "charge_salt_isotope_differs"
    assert result.comparable is True


def test_compare_missing_one_side_a() -> None:
    result = compare_structures(None, "CCO")
    assert result.level == "missing_one_side"
    assert result.comparable is False
    assert "B" in result.detail


def test_compare_missing_one_side_b() -> None:
    result = compare_structures("CCO", None)
    assert result.level == "missing_one_side"
    assert result.comparable is False
    assert "A" in result.detail


def test_compare_both_none() -> None:
    result = compare_structures(None, None)
    assert result.level == "uncomparable"
    assert result.comparable is False


def test_compare_empty_string() -> None:
    result = compare_structures("", "CCO")
    assert result.level == "missing_one_side"
    assert result.comparable is False


def test_compare_whitespace_only() -> None:
    result = compare_structures("   ", "CCO")
    assert result.level == "missing_one_side"
    assert result.comparable is False


def test_compare_uncomparable_parse_failure() -> None:
    result = compare_structures("not-smiles", "CCO")
    assert result.level == "uncomparable"
    assert result.comparable is False


def test_compare_uncomparable_both_invalid() -> None:
    result = compare_structures("bad-a", "bad-b")
    assert result.level == "uncomparable"
    assert result.comparable is False


def test_compare_no_rdkit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "rdkit", None)
    result = compare_structures("CCO", "CCC")
    assert result.level == "uncomparable"
    assert result.comparable is False
    assert "RDKit" in result.detail


def test_compare_strip_charge_normalization() -> None:
    result = compare_structures(
        "CC(=O)O", "CC(=O)[O-]", normalization="strip_charge"
    )
    assert result.level == "identical"
    assert result.comparable is True


def test_compare_strip_isotopes_normalization() -> None:
    result = compare_structures("[13C]O", "CO", normalization="strip_isotopes")
    assert result.level == "identical"
    assert result.comparable is True


def test_compare_strip_charge_no_effect_on_connectivity() -> None:
    result = compare_structures("CCO", "CCC", normalization="strip_charge")
    assert result.level == "connectivity_differs"
    assert result.comparable is True


# --- render_smiles_svg ---


def test_render_smiles_svg_valid() -> None:
    svg = render_smiles_svg("CCO")
    assert svg is not None
    assert "<svg" in svg


def test_render_smiles_svg_with_highlight() -> None:
    svg = render_smiles_svg("CCO", highlight_atoms=[0, 1])
    assert svg is not None
    assert "<svg" in svg


def test_render_smiles_svg_custom_size() -> None:
    svg = render_smiles_svg("CCO", size=(200, 150))
    assert svg is not None
    assert "<svg" in svg


def test_render_smiles_svg_invalid() -> None:
    assert render_smiles_svg("not-smiles") is None


def test_render_smiles_svg_no_rdkit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "rdkit", None)
    assert render_smiles_svg("CCO") is None


# --- dataclass properties ---


def test_stereo_center_frozen() -> None:
    center = StereoCenter(atom_index=0, assignment="R", specified=True)
    with pytest.raises(AttributeError):
        center.atom_index = 1  # type: ignore[misc]


def test_stereo_bond_frozen() -> None:
    bond = StereoBond(
        bond_index=0, begin_atom=0, end_atom=1, assignment="E", specified=True
    )
    with pytest.raises(AttributeError):
        bond.bond_index = 1  # type: ignore[misc]


def test_stereo_analysis_frozen() -> None:
    analysis = StereoAnalysis(
        canonical_smiles="CCO", centers=[], bonds=[], rdkit_available=True
    )
    with pytest.raises(AttributeError):
        analysis.canonical_smiles = "CCC"  # type: ignore[misc]


def test_structure_comparison_frozen() -> None:
    comp = StructureComparison(level="identical", detail="test", comparable=True)
    with pytest.raises(AttributeError):
        comp.level = "changed"  # type: ignore[misc]
