from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
CONSTITUTION = ROOT / ".autobuild" / "veriquant" / "BUILD_CONSTITUTION.md"


def test_constitution_preserves_certified_qlib_score_execution_in_v3() -> None:
    text = CONSTITUTION.read_text(encoding="utf-8")

    assert "Version: `build-constitution/v3`" in text
    assert "CertifiedQlibExpressionExecutor" in text
    assert "FactorScoreArtifact" in text
    assert "OFFICIAL_EVIDENCE" in text
    assert "Evidence Service" in text


def test_constitution_keeps_dynamic_execution_and_future_reference_closed() -> None:
    text = CONSTITUTION.read_text(encoding="utf-8")

    required_guards = (
        "Qlib 原生 `eval` 表达式解析",
        "自定义 Qlib Operator",
        "负数 `Ref`",
        "不得读取 label 或 sealed",
        "不得产生 official metrics",
    )
    for guard in required_guards:
        assert guard in text


def test_constitution_preserves_legacy_factor_dag_as_compatibility_only() -> None:
    text = CONSTITUTION.read_text(encoding="utf-8")

    assert "LegacyFactorDAGBackend" in text
    assert "只读兼容与回滚" in text
    assert "不得删除历史 FactorDAG artifact" in text
