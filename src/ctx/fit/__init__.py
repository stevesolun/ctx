"""CTX Fit — repository-specific AI coding stack optimization.

CTX Fit answers one question for one repository: which AI coding setup is most
effective and cost-efficient here, and what evidence proves it?

This package is a thin product layer over existing CTX capability. It reuses
CTX's repository intelligence, bounded capability selection, experiment
execution, and cost accounting rather than reimplementing them.
"""

from __future__ import annotations

from ctx.fit.profile import (
    FIT_PROFILE_SCHEMA,
    ExistingAiConfig,
    FitProfile,
    OptimizationDimension,
    build_fit_profile,
)
from ctx.fit.verification import (
    VerificationCommand,
    VerificationInventory,
    discover_verification,
)

__all__ = [
    "FIT_PROFILE_SCHEMA",
    "ExistingAiConfig",
    "FitProfile",
    "OptimizationDimension",
    "VerificationCommand",
    "VerificationInventory",
    "build_fit_profile",
    "discover_verification",
]
