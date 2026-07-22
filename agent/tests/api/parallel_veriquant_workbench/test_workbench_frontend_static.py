from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
PAGE = REPO_ROOT / "frontend" / "src" / "pages" / "VeriquantWorkbench.tsx"
PANELS = REPO_ROOT / "frontend" / "src" / "components" / "veriquant" / "WorkbenchPanels.tsx"
TYPES = REPO_ROOT / "frontend" / "src" / "components" / "veriquant" / "WorkbenchTypes.ts"
VITEST = REPO_ROOT / "frontend" / "src" / "pages" / "__tests__" / "VeriquantWorkbench.test.tsx"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_frontend_owned_files_exist() -> None:
    assert PAGE.exists()
    assert PANELS.exists()
    assert TYPES.exists()
    assert VITEST.exists()


def test_status_labels_and_required_panels_are_present() -> None:
    types = _read(TYPES)
    panels = _read(PANELS)
    page = _read(PAGE)
    for status in [
        "DRAFT",
        "VALIDATED_RESEARCH",
        "CHALLENGER",
        "SHADOW_ACTIVE",
        "REJECTED",
        "INCOMPLETE",
        "BLOCKED",
    ]:
        assert status in types
    for label in [
        "Run Overview",
        "Research DAG",
        "Candidate Lineage",
        "Evidence Audit",
        "Source/Citation Timeline",
        "Promotion Status",
        "Shadow Portfolio",
        "Failure/Drift Alerts",
    ]:
        assert label in panels or label in page


def test_semantic_isolation_warnings_are_visible_text() -> None:
    panels = _read(PANELS)
    for phrase in [
        "Source-backed hypothesis is not alpha evidence.",
        "DRAFT factor is not CHALLENGER.",
        "Shadow Portfolio is not a real account.",
        "Sealed data shows only redacted decision or failure code.",
        "News citation is not a tradable signal.",
    ]:
        assert phrase in panels


def test_no_untrusted_html_rendering_or_trading_surface() -> None:
    combined = "\n".join(_read(path) for path in [PAGE, PANELS, TYPES])
    forbidden = [
        "dangerouslySetInnerHTML",
        "innerHTML",
        "eval(",
        "exec(",
        "placeOrder",
        "sendOrder",
        "Authorization",
        "api_key",
        "token",
        "cookie",
    ]
    for needle in forbidden:
        assert needle not in combined


def test_page_does_not_modify_router_or_shared_api() -> None:
    page = _read(PAGE)
    assert 'from "@/lib/api"' not in page
    assert "react-router" not in page
    assert '"/veriquant/workbench"' in page

