"""Static VeriQuant factor-library catalog.

This package is intentionally metadata-only.  It never imports or executes the
legacy factor zoo modules on the trusted VeriQuant path.
"""

from .catalog import (
    CatalogBuildError,
    CatalogFailureCode,
    FactorLibraryCatalog,
    FactorLibraryCatalogBuilder,
    FactorLibraryRecord,
    FactorQualificationStatus,
    build_factor_library_catalog,
)

__all__ = [
    "CatalogBuildError",
    "CatalogFailureCode",
    "FactorLibraryCatalog",
    "FactorLibraryCatalogBuilder",
    "FactorLibraryRecord",
    "FactorQualificationStatus",
    "build_factor_library_catalog",
]
