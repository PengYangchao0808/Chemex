"""Small RDKit rendering helper used only by review generation."""

from __future__ import annotations

import io


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
