"""Local import compatibility for the Agent-B isolated test package."""

from veriquant.real_research import api as research_api
from veriquant.real_research.reader import build_reader_manifest


# The public api intentionally omits this construction helper.  Bind it only in
# this isolated test process so fixtures can rebuild a content-bound envelope.
research_api.build_reader_manifest = build_reader_manifest
