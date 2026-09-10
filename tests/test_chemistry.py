"""Tests for stereo analysis, structure comparison, and SVG rendering."""

from __future__ import annotations

import re
import sys

import pytest

from chemex_lit.chemistry import (
    StereoAnalysis,
    StereoBond,
    StereoCenter,
    StructureComparison,
    analyze_stereo,
    asset_basename,
    compare_structures,
    drawing_config_hash,
    gold_highlight_atoms,
    render_smiles_svg,
    render_structure,
    structure_hash,
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


# --- render_structure ---


def test_render_structure_ok() -> None:
    result = render_structure("CCO")
    assert result.status == "ok"
    assert result.png is not None
    assert result.png.startswith(b"\x89PNG")
    assert result.svg is not None
    assert "<svg" in result.svg


def test_render_structure_missing_none() -> None:
    result = render_structure(None)
    assert result.status == "missing_smiles"
    assert result.png is None
    assert result.svg is None


def test_render_structure_missing_empty() -> None:
    result = render_structure("")
    assert result.status == "missing_smiles"


def test_render_structure_missing_whitespace() -> None:
    result = render_structure("   ")
    assert result.status == "missing_smiles"


def test_render_structure_invalid_smiles() -> None:
    result = render_structure("not-smiles")
    assert result.status == "invalid_smiles"
    assert result.png is None
    assert result.svg is None


def test_render_structure_rdkit_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "rdkit", None)
    result = render_structure("CCO")
    assert result.status == "rdkit_missing"
    assert result.png is None
    assert result.svg is None


def test_render_structure_stereo_mode() -> None:
    normal = render_structure("C[C@H](O)CC", mode="normal")
    stereo = render_structure("C[C@H](O)CC", mode="stereo")
    assert normal.status == "ok"
    assert stereo.status == "ok"
    assert normal.svg is not None
    assert stereo.svg is not None
    assert normal.svg != stereo.svg
    assert "Stereo" in stereo.svg or "stereo" in stereo.svg or stereo.svg != normal.svg


def test_render_structure_atommap_mode() -> None:
    normal = render_structure("CCO", mode="normal")
    atommap = render_structure("CCO", mode="atommap")
    assert normal.status == "ok"
    assert atommap.status == "ok"
    assert normal.svg is not None
    assert atommap.svg is not None
    assert normal.svg != atommap.svg


def test_render_structure_highlight_atoms() -> None:
    result = render_structure("CCO", highlight_atoms=[0, 1])
    assert result.status == "ok"
    assert result.png is not None
    assert result.svg is not None


def test_render_structure_png_2x() -> None:
    result = render_structure("CCO", size=(200, 150))
    assert result.status == "ok"
    assert result.png is not None
    assert result.svg is not None


def test_render_structure_stereo_unassigned_highlight() -> None:
    result = render_structure("CC(O)F", mode="stereo")
    assert result.status == "ok"
    assert result.svg is not None


# --- structure_hash ---


def test_structure_hash_valid() -> None:
    h = structure_hash("CCO")
    assert h is not None
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)


def test_structure_hash_deterministic() -> None:
    assert structure_hash("CCO") == structure_hash("CCO")
    assert structure_hash("OCC") == structure_hash("CCO")


def test_structure_hash_differs_for_molecules() -> None:
    assert structure_hash("CCO") != structure_hash("CCC")


def test_structure_hash_invalid() -> None:
    assert structure_hash("not-smiles") is None


# --- drawing_config_hash ---


def test_drawing_config_hash_deterministic() -> None:
    h1 = drawing_config_hash("normal", (420, 280))
    h2 = drawing_config_hash("normal", (420, 280))
    assert h1 == h2
    assert len(h1) == 8
    assert all(c in "0123456789abcdef" for c in h1)


def test_drawing_config_hash_differs_by_mode() -> None:
    assert drawing_config_hash("normal", (420, 280)) != drawing_config_hash(
        "stereo", (420, 280)
    )


def test_drawing_config_hash_differs_by_size() -> None:
    assert drawing_config_hash("normal", (420, 280)) != drawing_config_hash(
        "normal", (200, 150)
    )


# --- asset_basename ---


def test_asset_basename_deterministic() -> None:
    b1 = asset_basename("CCO", "normal")
    b2 = asset_basename("CCO", "normal")
    assert b1 == b2


def test_asset_basename_format() -> None:
    b = asset_basename("CCO", "normal")
    assert b is not None
    assert re.match(r"^[0-9a-f]{16}-[0-9a-f]{8}$", b)


def test_asset_basename_differs_by_mode() -> None:
    assert asset_basename("CCO", "normal") != asset_basename("CCO", "stereo")


def test_asset_basename_differs_by_molecule() -> None:
    assert asset_basename("CCO", "normal") != asset_basename("CCC", "normal")


def test_asset_basename_none_for_invalid() -> None:
    assert asset_basename("not-smiles", "normal") is None


# --- gold_highlight_atoms ---


def test_gold_highlight_identical() -> None:
    result = gold_highlight_atoms("CCO", "CCO")
    assert result is not None
    assert result == ([], [])


def test_gold_highlight_enantiomer() -> None:
    result = gold_highlight_atoms("C[C@H](O)CC", "C[C@@H](O)CC")
    assert result is not None
    extracted, gold = result
    assert len(extracted) > 0
    assert len(gold) > 0
    assert 1 in extracted


def test_gold_highlight_no_overlap() -> None:
    result = gold_highlight_atoms("CCO", "c1ccccc1")
    assert result is None


def test_gold_highlight_unparseable_a() -> None:
    assert gold_highlight_atoms("not-smiles", "CCO") is None


def test_gold_highlight_unparseable_b() -> None:
    assert gold_highlight_atoms("CCO", "not-smiles") is None


def test_gold_highlight_both_unparseable() -> None:
    assert gold_highlight_atoms("bad-a", "bad-b") is None


def test_gold_highlight_same_connectivity_diff_stereo() -> None:
    result = gold_highlight_atoms("C/C=C/C", "C/C=C\\C")
    assert result is not None
    extracted, gold = result
    assert len(extracted) > 0
    assert len(gold) > 0
