"""Safe Research OS publication repository for AlphaForge.

The package writes already-produced formal reports and acceptance hashes into a
redacted artifact repository. It performs no financial computation and registers
no public API or frontend route.
"""

from .models import (
    LatestPublicationPointer,
    PublicationArtifactKind,
    PublicationArtifactRef,
    PublicationStatus,
    ResearchOSArtifactManifest,
    ResearchOSPublicationReceipt,
    assert_publication_payload_safe,
    require_safe_run_id,
)
from .repository import ResearchOSPublicationRepository

__all__ = [
    "LatestPublicationPointer",
    "PublicationArtifactKind",
    "PublicationArtifactRef",
    "PublicationStatus",
    "ResearchOSArtifactManifest",
    "ResearchOSPublicationReceipt",
    "ResearchOSPublicationRepository",
    "assert_publication_payload_safe",
    "require_safe_run_id",
]

