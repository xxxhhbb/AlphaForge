from __future__ import annotations

from veriquant.formula_semantics import FormulaFindingSeverity, FormulaSemanticReviewer


def source(fields, expression):
    return (
        "def run_research(train_rows, prediction_rows, seed):\n"
        f"    feature_fields = {fields!r}\n"
        "    z_scores = [1.0 for _ in feature_fields]\n"
        f"    score = {expression}\n"
        "    return {'rows': []}\n"
    )


def rows(**columns):
    result = []
    count = len(next(iter(columns.values())))
    for index in range(count):
        result.append({
            "decision_time": f"2026-01-{1 + index // 3:02d}",
            **{name: values[index] for name, values in columns.items()},
        })
    return result


def review(text, values):
    return FormulaSemanticReviewer().review(
        source_text=text,
        output_kind="ScoreTable",
        feature_rows=values,
        data_snapshot_hash="sha256:" + "d" * 64,
    )


def test_renamed_same_date_constant_additive_field_requires_revision() -> None:
    result = review(
        source(["random_feature", "turnover_rate"], "z_scores[0] + z_scores[1]"),
        rows(
            random_feature=[1, 1, 1, 2, 2, 2],
            turnover_rate=[1, 2, 3, 2, 4, 8],
        ),
    )
    assert result.requires_revision
    assert result.findings[0].code == "CROSS_SECTION_CONSTANT_ADDITIVE_TERM"
    assert result.findings[0].field_names == ("random_feature",)


def test_macro_named_field_that_varies_cross_section_is_not_rejected() -> None:
    result = review(
        source(["shibor_1w", "turnover_rate"], "z_scores[0] + z_scores[1]"),
        rows(
            shibor_1w=[1, 2, 3, 2, 3, 4],
            turnover_rate=[3, 1, 2, 4, 2, 1],
        ),
    )
    assert not result.requires_revision


def test_same_date_constant_field_in_interaction_is_allowed() -> None:
    result = review(
        source(["macro_state", "turnover_rate"], "z_scores[0] * z_scores[1]"),
        rows(
            macro_state=[1, 1, 1, 2, 2, 2],
            turnover_rate=[1, 2, 3, 2, 4, 8],
        ),
    )
    assert not result.requires_revision


def test_high_correlation_is_advisory_and_equal_weight_is_not_rejected() -> None:
    result = review(
        source(["value_a", "value_b"], "(z_scores[0] + z_scores[1]) / 2.0"),
        rows(
            value_a=[1, 2, 3, 4, 5, 6],
            value_b=[2, 4, 6, 8, 10, 12],
        ),
    )
    assert not result.requires_revision
    assert any(
        finding.code == "HIGH_FEATURE_CORRELATION"
        and finding.severity is FormulaFindingSeverity.ADVISORY
        for finding in result.findings
    )


def test_review_hash_is_stable() -> None:
    text = source(["a", "b"], "z_scores[0] - z_scores[1]")
    frozen = rows(a=[1, 2, 3, 2, 4, 8], b=[3, 1, 2, 4, 2, 1])
    assert review(text, frozen).artifact_hash == review(text, frozen).artifact_hash


def test_sum_of_z_scores_is_also_semantically_reviewed() -> None:
    result = review(
        source(["constant_state", "turnover_rate"], "sum(z_scores) / len(z_scores)"),
        rows(
            constant_state=[1, 1, 1, 2, 2, 2],
            turnover_rate=[1, 2, 3, 2, 4, 8],
        ),
    )
    assert result.requires_revision
    assert result.findings[0].field_names == ("constant_state",)
