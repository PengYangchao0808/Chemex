from __future__ import annotations

from chemex_lit.assembly import Assembler
from chemex_lit.chemistry import Validator
from chemex_lit.models import CompoundRef, ReactionCandidate, StructureCandidate


def reaction(candidate_id: str = "r1", source: str = "text") -> ReactionCandidate:
    return ReactionCandidate(
        candidate_id=candidate_id,
        source=source,
        reactants=[CompoundRef(label="7", role="reactant")],
        products=[CompoundRef(label="8", role="product")],
        evidence_ids=["text-p1"],
        yield_pct=80,
        confidence=0.9,
    )


def structures() -> list[StructureCandidate]:
    return [
        StructureCandidate(candidate_id="s7", compound_label="7", smiles="CC"),
        StructureCandidate(candidate_id="s8", compound_label="8", smiles="CCO"),
    ]


def test_validator_reports_invalid_smiles() -> None:
    invalid = [StructureCandidate(candidate_id="bad", compound_label="8", smiles="not-smiles")]
    outcome = Validator().validate([reaction()], invalid)
    assert any(issue.code == "V001_SMILES_PARSE_FAILED" for issue in outcome.issues)


def test_validator_detects_label_conflict() -> None:
    conflicting = [
        StructureCandidate(candidate_id="a", compound_label="8", smiles="CC"),
        StructureCandidate(candidate_id="b", compound_label="8", smiles="CCC"),
    ]
    outcome = Validator().validate([reaction()], conflicting)
    assert sum(issue.code == "V005_LABEL_STRUCTURE_CONFLICT" for issue in outcome.issues) == 2


def test_assembler_resolves_structures_and_accepts_high_confidence_record() -> None:
    candidates = [reaction("text", "text"), reaction("table", "table")]
    outcome = Validator().validate(candidates, structures())
    records = Assembler().assemble(candidates, structures(), outcome)
    assert len(records) == 1
    assert records[0].products[0].smiles == "CCO"
    assert records[0].review_status == "accepted"


def test_assembler_sends_missing_structure_to_review() -> None:
    outcome = Validator().validate([reaction()], [])
    record = Assembler().assemble([reaction()], [], outcome)[0]
    assert record.review_status == "needs_review"
    assert any(issue.code == "A002_STRUCTURE_MISSING" for issue in record.issues)
