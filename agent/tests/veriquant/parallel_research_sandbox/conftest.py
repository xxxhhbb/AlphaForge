from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
AGENT_ROOT = ROOT / "agent"
SRC_ROOT = AGENT_ROOT / "src"
for item in (str(SRC_ROOT), str(AGENT_ROOT)):
    if item not in sys.path:
        sys.path.insert(0, item)
