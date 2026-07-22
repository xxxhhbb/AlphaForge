from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
CONSTITUTION = ROOT / ".autobuild" / "veriquant" / "BUILD_CONSTITUTION.md"


def test_constitution_explicitly_authorizes_only_isolated_research_python() -> None:
    text = CONSTITUTION.read_text(encoding="utf-8")
    assert "Amendment `A-02`" in text
    assert "generated research Python" in text
    assert "DRAFT" in text and "AUXILIARY_RESEARCH_EVIDENCE" in text
    assert "pinned no-network Research Sandbox" in text
    assert "standard output contract" in text
    assert "independent deterministic evaluator" in text


def test_research_sandbox_amendment_preserves_forbidden_authorities() -> None:
    text = CONSTITUTION.read_text(encoding="utf-8")
    amendment = text.split("Amendment `A-02`", 1)[1].split("## 1.", 1)[0]
    for boundary in (
        "sealed",
        "Promotion",
        "portfolio",
        "risk",
        "order",
        "ledger",
        "network",
        "secret",
    ):
        assert boundary in amendment
