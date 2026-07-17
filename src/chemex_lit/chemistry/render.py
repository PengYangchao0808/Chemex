"""Small RDKit rendering helper used only by review generation."""

from __future__ import annotations

from pathlib import Path


def render_smiles(smiles: str, output: Path, size: tuple[int, int] = (360, 240)) -> bool:
    try:
        from rdkit import Chem
        from rdkit.Chem import Draw  # pyright: ignore[reportAttributeAccessIssue]

        molecule = Chem.MolFromSmiles(smiles)
        if molecule is None:
            return False
        output.parent.mkdir(parents=True, exist_ok=True)
        Draw.MolToFile(molecule, str(output), size=size)
        return output.is_file()
    except (ImportError, ValueError, OSError):
        return False
