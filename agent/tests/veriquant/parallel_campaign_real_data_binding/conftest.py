from __future__ import annotations

import sys
from pathlib import Path


AGENT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = AGENT_ROOT / "src"
for candidate in (SRC_ROOT, AGENT_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))
