"""Stable public imports for the host-neutral engine coordinator."""

from ctx.engine import (
    CapabilityInstallPlanPort,
    CtxEngine,
    CtxEngineError,
    EngineSnapshot,
    InstallConsentPolicy,
    InstallPlanDescriptor,
    PreparedInstallPlan,
    ReplayDivergenceError,
    SnapshotContentionError,
    UnsupportedReducerVersionError,
)


def test_engine_coordinator_and_public_errors_are_exported() -> None:
    assert CtxEngine.__name__ == "CtxEngine"
    assert EngineSnapshot.__name__ == "EngineSnapshot"
    assert issubclass(ReplayDivergenceError, CtxEngineError)
    assert issubclass(SnapshotContentionError, CtxEngineError)
    assert issubclass(UnsupportedReducerVersionError, CtxEngineError)


def test_installation_contract_is_exported_from_the_stable_engine_package() -> None:
    assert CapabilityInstallPlanPort.__name__ == "CapabilityInstallPlanPort"
    assert InstallConsentPolicy.safe_default().skill_mode == "ask-each-time"
    assert InstallPlanDescriptor.__name__ == "InstallPlanDescriptor"
    assert PreparedInstallPlan.__name__ == "PreparedInstallPlan"
