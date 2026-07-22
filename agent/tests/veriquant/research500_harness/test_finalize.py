from __future__ import annotations

from veriquant.research500_harness.finalize import _scan_source


def test_source_scan_accepts_bounded_numeric_research() -> None:
    findings = []
    _scan_source(
        "import math\nimport numpy as np\ndef run_research(training_rows, prediction_rows, context):\n return []\n",
        question_id="Q001",
        findings=findings,
    )
    assert findings == []


def test_source_scan_rejects_network_path_and_arbitrary_execution() -> None:
    findings = []
    _scan_source(
        "import requests\nfrom pathlib import Path\ndef run_research(a,b,c):\n return eval(open('x').read())\n",
        question_id="Q001",
        findings=findings,
    )
    assert {item["code"] for item in findings} == {
        "NETWORK_OR_PATH_CAPABILITY", "ARBITRARY_EXECUTION"
    }
