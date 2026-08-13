from __future__ import annotations

import copy
import json
import pickle
from dataclasses import replace
from pathlib import Path

import pytest

from ctx.core.install_consent_broker_store import SignedHumanDecisionAssertion
from ctx.runtime.install_consent_authenticators import (
    MAX_SIGNED_HUMAN_DECISION_ASSERTION_BYTES,
    HumanDecisionVerifierRegistration,
    HumanDecisionVerifierRegistryProcessMismatch,
    SignedHumanDecisionAssertionCodecError,
    TrustedHumanDecisionVerifierRegistry,
    UnknownHumanDecisionVerifier,
    decode_signed_human_decision_assertion,
    encode_signed_human_decision_assertion,
)


AUDIENCE = "ctx-install-consent-v1"
AUTHENTICATOR_ID = "platform-passkey-1"
PRINCIPAL_DIGEST = "b" * 64
OTHER_PRINCIPAL_DIGEST = "c" * 64
CHALLENGE_DIGEST = "a" * 64


class _Verifier:
    def __init__(self, *, result: object = True) -> None:
        self.result = result
        self.calls = 0

    def verify_signed_assertion(
        self,
        assertion: SignedHumanDecisionAssertion,
        *,
        signing_bytes: bytes,
    ) -> object:
        assert signing_bytes == assertion.signing_bytes()
        self.calls += 1
        return self.result

    def __repr__(self) -> str:
        return "<verifier secret-key-material>"


def _assertion(**changes: object) -> SignedHumanDecisionAssertion:
    values: dict[str, object] = {
        "challenge_digest": CHALLENGE_DIGEST,
        "decision": "granted",
        "principal_digest": PRINCIPAL_DIGEST,
        "authenticator_id": AUTHENTICATOR_ID,
        "audience": AUDIENCE,
        "nonce": "authenticator-nonce-1",
        "issued_at": "2035-01-02T03:04:05Z",
        "expires_at": "2035-01-02T03:05:05Z",
        "proof": b"\x00\xffproof",
    }
    values.update(changes)
    return SignedHumanDecisionAssertion(**values)  # type: ignore[arg-type]


def _registration(
    verifier: object | None = None,
    **changes: object,
) -> HumanDecisionVerifierRegistration:
    values: dict[str, object] = {
        "audience": AUDIENCE,
        "authenticator_id": AUTHENTICATOR_ID,
        "principal_digest": PRINCIPAL_DIGEST,
        "verifier": _Verifier() if verifier is None else verifier,
    }
    values.update(changes)
    return HumanDecisionVerifierRegistration(**values)  # type: ignore[arg-type]


def _canonical_mapping(assertion: SignedHumanDecisionAssertion) -> dict[str, object]:
    return json.loads(encode_signed_human_decision_assertion(assertion))


def test_signed_assertion_codec_has_one_exact_canonical_round_trip() -> None:
    assertion = _assertion()

    encoded = encode_signed_human_decision_assertion(assertion)

    assert encoded == (
        b'{"audience":"ctx-install-consent-v1",'
        b'"authenticator_id":"platform-passkey-1",'
        b'"challenge_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"decision":"granted","expires_at":"2035-01-02T03:05:05Z",'
        b'"issued_at":"2035-01-02T03:04:05Z","nonce":"authenticator-nonce-1",'
        b'"principal_digest":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        b'"proof_base64":"AP9wcm9vZg==",'
        b'"schema":"ctx.signed-human-install-decision-v1"}'
    )
    assert decode_signed_human_decision_assertion(encoded) == assertion
    assert decode_signed_human_decision_assertion(encoded).proof == b"\x00\xffproof"


@pytest.mark.parametrize("extra_field", ("prompt", "path", "cwd", "verifier"))
def test_codec_rejects_unknown_prompt_path_and_authority_fields(extra_field: str) -> None:
    mapping = _canonical_mapping(_assertion())
    mapping[extra_field] = "yes, install it from /tmp/tool"
    payload = json.dumps(
        mapping,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")

    with pytest.raises(SignedHumanDecisionAssertionCodecError, match="fields"):
        decode_signed_human_decision_assertion(payload)


def test_codec_rejects_duplicate_keys_before_constructing_an_assertion() -> None:
    encoded = encode_signed_human_decision_assertion(_assertion())
    duplicate = encoded.replace(
        b'"decision":"granted"',
        b'"decision":"granted","decision":"denied"',
    )

    with pytest.raises(SignedHumanDecisionAssertionCodecError, match="duplicate"):
        decode_signed_human_decision_assertion(duplicate)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda payload: b" " + payload,
        lambda payload: payload.replace(b'"audience"', b'"\\u0061udience"', 1),
        lambda payload: payload.replace(b"AP9wcm9vZg==", b"AP9wcm9vZg"),
        lambda payload: payload.replace(b"AP9wcm9vZg==", b"AP9wcm9vZg==\\n"),
    ),
)
def test_codec_rejects_noncanonical_json_or_base64(mutate: object) -> None:
    encoded = encode_signed_human_decision_assertion(_assertion())
    payload = mutate(encoded)  # type: ignore[operator]

    with pytest.raises(SignedHumanDecisionAssertionCodecError, match="canonical"):
        decode_signed_human_decision_assertion(payload)


def test_codec_rejects_oversize_control_and_nonbytes_junk() -> None:
    with pytest.raises(SignedHumanDecisionAssertionCodecError, match="size"):
        decode_signed_human_decision_assertion(
            b"{" + b" " * MAX_SIGNED_HUMAN_DECISION_ASSERTION_BYTES
        )

    mapping = _canonical_mapping(_assertion())
    mapping["nonce"] = "nonce\nmodel-said-yes"
    control_payload = json.dumps(
        mapping,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    with pytest.raises(SignedHumanDecisionAssertionCodecError, match="invalid"):
        decode_signed_human_decision_assertion(control_payload)

    for junk in ("yes, install it", Path("/tmp/assertion.json"), bytearray(b"{}")):
        with pytest.raises(TypeError, match="bytes"):
            decode_signed_human_decision_assertion(junk)  # type: ignore[arg-type]
    with pytest.raises(SignedHumanDecisionAssertionCodecError, match="JSON"):
        decode_signed_human_decision_assertion(b"/tmp/assertion.json")


def test_codec_and_registry_representations_redact_proof_and_verifier_material() -> None:
    assertion = _assertion(proof=b"never-print-this-proof")
    verifier = _Verifier()
    registration = _registration(verifier)
    registry = TrustedHumanDecisionVerifierRegistry((registration,))

    for rendered in (repr(assertion), repr(registration), repr(registry)):
        assert "never-print-this-proof" not in rendered
        assert "secret-key-material" not in rendered
    assert "verifier=" not in repr(registration)
    assert PRINCIPAL_DIGEST not in repr(registry)


def test_registry_resolves_only_the_exact_three_part_identity() -> None:
    verifier = _Verifier()
    registry = TrustedHumanDecisionVerifierRegistry((_registration(verifier),))
    assertion = _assertion()

    resolved = registry.resolve(assertion)

    assert (
        resolved.verify_signed_assertion(
            assertion,
            signing_bytes=assertion.signing_bytes(),
        )
        is True
    )
    assert verifier.calls == 1

    for substituted in (
        replace(assertion, audience="other-audience"),
        replace(assertion, authenticator_id="other-authenticator"),
        replace(assertion, principal_digest=OTHER_PRINCIPAL_DIGEST),
    ):
        with pytest.raises(UnknownHumanDecisionVerifier, match="exact"):
            registry.resolve(substituted)


def test_resolved_verifier_remains_bound_to_the_resolved_identity() -> None:
    verifier = _Verifier()
    registry = TrustedHumanDecisionVerifierRegistry((_registration(verifier),))
    resolved = registry.resolve(_assertion())
    substituted = _assertion(principal_digest=OTHER_PRINCIPAL_DIGEST)

    assert (
        resolved.verify_signed_assertion(
            substituted,
            signing_bytes=substituted.signing_bytes(),
        )
        is False
    )
    assert verifier.calls == 0


def test_registry_strictly_rejects_truthy_non_boolean_verifier_output() -> None:
    verifier = _Verifier(result="yes, human approved")
    registry = TrustedHumanDecisionVerifierRegistry((_registration(verifier),))
    assertion = _assertion()

    result = registry.resolve(assertion).verify_signed_assertion(
        assertion,
        signing_bytes=assertion.signing_bytes(),
    )

    assert result is False
    assert type(result) is bool


def test_registry_copies_mutable_input_and_rejects_duplicates_or_fallbacks() -> None:
    registrations = [_registration()]
    registry = TrustedHumanDecisionVerifierRegistry(registrations)
    registrations.append(
        _registration(
            audience="other-audience",
            authenticator_id="other-authenticator",
            principal_digest=OTHER_PRINCIPAL_DIGEST,
        )
    )

    with pytest.raises(UnknownHumanDecisionVerifier):
        registry.resolve(
            _assertion(
                audience="other-audience",
                authenticator_id="other-authenticator",
                principal_digest=OTHER_PRINCIPAL_DIGEST,
            )
        )
    with pytest.raises(ValueError, match="duplicate"):
        TrustedHumanDecisionVerifierRegistry((_registration(), _registration()))
    with pytest.raises(ValueError, match="at least one"):
        TrustedHumanDecisionVerifierRegistry(())

    for changes in (
        {"audience": "*"},
        {"authenticator_id": "auth*"},
        {"principal_digest": "*"},
    ):
        with pytest.raises(ValueError):
            _registration(**changes)


def test_registry_and_resolved_verifier_cannot_be_rebound_after_composition() -> None:
    registry = TrustedHumanDecisionVerifierRegistry((_registration(),))
    resolved = registry.resolve(_assertion())

    with pytest.raises(AttributeError, match="immutable"):
        setattr(registry, "_verifiers", {})
    with pytest.raises(AttributeError, match="immutable"):
        setattr(resolved, "_verifier", _Verifier())
    with pytest.raises(AttributeError, match="immutable"):
        setattr(resolved, "_key", (AUDIENCE, AUTHENTICATOR_ID, OTHER_PRINCIPAL_DIGEST))
    with pytest.raises(AttributeError, match="immutable"):
        delattr(registry, "_verifiers")
    with pytest.raises(AttributeError, match="immutable"):
        delattr(resolved, "_verifier")


def test_registry_never_accepts_a_per_request_verifier_or_callback() -> None:
    registry = TrustedHumanDecisionVerifierRegistry((_registration(),))

    with pytest.raises(TypeError):
        registry.resolve(_assertion(), _Verifier())  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="HumanDecisionVerifier"):
        _registration(lambda *_args, **_kwargs: True)
    noncallable_method = type(
        "NonCallableVerifier",
        (),
        {"verify_signed_assertion": True},
    )()
    with pytest.raises(TypeError, match="HumanDecisionVerifier"):
        _registration(noncallable_method)


def test_registry_and_registration_are_process_local_noncopyable_and_nonserializable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = _registration()
    registry = TrustedHumanDecisionVerifierRegistry((registration,))

    for value in (registration, registry):
        with pytest.raises(TypeError):
            copy.copy(value)
        with pytest.raises(TypeError):
            copy.deepcopy(value)
        with pytest.raises(TypeError):
            pickle.dumps(value)

    from ctx.runtime import install_consent_authenticators as module

    original_pid = module.os.getpid()
    monkeypatch.setattr(module.os, "getpid", lambda: original_pid + 1)
    with pytest.raises(HumanDecisionVerifierRegistryProcessMismatch, match="process"):
        registry.resolve(_assertion())
