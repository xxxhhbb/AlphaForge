"""Normalize the shared adversarial fixture before Agent-B test collection.

The shared real-research fixture intentionally contains one incomplete market
state to test row-level rejection.  Agent B's package has a stronger run-level
preflight, so its normal smoke fixture fills that one value.  Individual tests
can still replace a field with ``None`` to verify fail-closed behavior.
"""

from dataclasses import replace

from veriquant.real_research import api as research_api
from veriquant.real_research.fixture import build_offline_real_research_fixture as _build_fixture
from veriquant.real_research.reader import FrozenSnapshotEnvelope


def _complete_fixture(*args, **kwargs):
    fixture = _build_fixture(*args, **kwargs)
    states = tuple(
        replace(state, limit_up=False) if state.limit_up is None else state
        for state in fixture.envelope.row_market_state
    )
    envelope = FrozenSnapshotEnvelope(
        manifest=fixture.envelope.manifest,
        snapshot=fixture.snapshot,
        row_market_state=states,
    )
    return replace(fixture, envelope=envelope)


research_api.build_offline_real_research_fixture = _complete_fixture
