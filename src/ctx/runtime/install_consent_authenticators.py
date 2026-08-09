"""Trusted-composition boundary for human install-consent authenticators.

This module only binds an already configured authenticator verifier to one
exact audience, authenticator identity, and principal digest.  It does not
authenticate a human by itself and intentionally ships no permissive or
shared-secret verifier.  Production hosts must supply an external WebAuthn,
OS user-verification, or equivalently strong adapter at composition time.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, NoReturn, SupportsIndex

from ctx.core.install_consent_broker_store import (
    HumanDecisionVerifier,
    SignedHumanDecisionAssertion,
)


SIGNED_HUMAN_DECISION_ASSERTION_SCHEMA: Final = "ctx.signed-human-install-decision-v1"
MAX_SIGNED_HUMAN_DECISION_ASSERTION_BYTES: Final = 16_384
_DIGEST_RE: Final = re.compile(r"\A[0-9a-f]{64}\Z")
_TOKEN_RE: Final = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
_ASSERTION_FIELDS: Final = frozenset(
    {
        "audience",
        "authenticator_id",
        "challenge_digest",
        "decision",
        "expires_at",
        "issued_at",
        "nonce",
        "principal_digest",
        "proof_base64",
        "schema",
    }
)


class SignedHumanDecisionAssertionCodecError(ValueError):
    """Serialized assertion data violates the bounded canonical contract."""


class UnknownHumanDecisionVerifier(LookupError):
    """No trusted verifier is registered for an assertion's exact identity."""


class HumanDecisionVerifierRegistryProcessMismatch(RuntimeError):
    """A process-local verifier registration or registry crossed a fork."""


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def encode_signed_human_decision_assertion(
    assertion: SignedHumanDecisionAssertion,
) -> bytes:
    """Encode one assertion using the only accepted wire representation."""

    if type(assertion) is not SignedHumanDecisionAssertion:
        raise TypeError("assertion must be a SignedHumanDecisionAssertion")
    encoded = _canonical_json(
        {
            "audience": assertion.audience,
            "authenticator_id": assertion.authenticator_id,
            "challenge_digest": assertion.challenge_digest,
            "decision": assertion.decision,
            "expires_at": assertion.expires_at,
            "issued_at": assertion.issued_at,
            "nonce": assertion.nonce,
            "principal_digest": assertion.principal_digest,
            "proof_base64": base64.b64encode(assertion.proof).decode("ascii"),
            "schema": SIGNED_HUMAN_DECISION_ASSERTION_SCHEMA,
        }
    )
    if len(encoded) > MAX_SIGNED_HUMAN_DECISION_ASSERTION_BYTES:
        raise SignedHumanDecisionAssertionCodecError(
            "signed human decision assertion exceeds its bounded size"
        )
    return encoded


def _reject_duplicate_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SignedHumanDecisionAssertionCodecError(
                "signed human decision assertion contains a duplicate JSON key"
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> NoReturn:
    raise SignedHumanDecisionAssertionCodecError(
        f"signed human decision assertion contains noncanonical JSON constant {value}"
    )


def _decode_canonical_proof(value: object) -> bytes:
    if not isinstance(value, str):
        raise SignedHumanDecisionAssertionCodecError("proof_base64 must be canonical base64 text")
    try:
        encoded = value.encode("ascii")
        proof = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise SignedHumanDecisionAssertionCodecError(
            "proof_base64 must be canonical base64 text"
        ) from None
    if base64.b64encode(proof) != encoded:
        raise SignedHumanDecisionAssertionCodecError("proof_base64 must be canonical base64 text")
    return proof


def decode_signed_human_decision_assertion(
    payload: bytes,
) -> SignedHumanDecisionAssertion:
    """Decode only exact, bounded canonical JSON with standard padded base64."""

    if type(payload) is not bytes:
        raise TypeError("signed human decision assertion payload must be bytes")
    if not 1 <= len(payload) <= MAX_SIGNED_HUMAN_DECISION_ASSERTION_BYTES:
        raise SignedHumanDecisionAssertionCodecError(
            "signed human decision assertion payload has an invalid size"
        )
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except SignedHumanDecisionAssertionCodecError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise SignedHumanDecisionAssertionCodecError(
            "signed human decision assertion payload is not valid JSON"
        ) from None

    if not isinstance(decoded, dict):
        raise SignedHumanDecisionAssertionCodecError(
            "signed human decision assertion JSON must be an object"
        )
    if set(decoded) != _ASSERTION_FIELDS:
        raise SignedHumanDecisionAssertionCodecError(
            "signed human decision assertion has invalid fields"
        )
    if decoded.get("schema") != SIGNED_HUMAN_DECISION_ASSERTION_SCHEMA:
        raise SignedHumanDecisionAssertionCodecError(
            "signed human decision assertion has an invalid schema"
        )

    proof = _decode_canonical_proof(decoded["proof_base64"])
    try:
        assertion = SignedHumanDecisionAssertion(
            challenge_digest=decoded["challenge_digest"],
            decision=decoded["decision"],
            principal_digest=decoded["principal_digest"],
            authenticator_id=decoded["authenticator_id"],
            audience=decoded["audience"],
            nonce=decoded["nonce"],
            issued_at=decoded["issued_at"],
            expires_at=decoded["expires_at"],
            proof=proof,
        )
    except (TypeError, ValueError):
        raise SignedHumanDecisionAssertionCodecError(
            "signed human decision assertion fields are invalid"
        ) from None
    if encode_signed_human_decision_assertion(assertion) != payload:
        raise SignedHumanDecisionAssertionCodecError(
            "signed human decision assertion payload is not canonical"
        )
    return assertion


def _token(value: object, name: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded canonical token")
    return value


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class HumanDecisionVerifierRegistration:
    """Process-local exact identity binding supplied by trusted composition."""

    audience: str
    authenticator_id: str
    principal_digest: str
    verifier: HumanDecisionVerifier = field(repr=False, compare=False)
    _pid: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _token(self.audience, "audience")
        _token(self.authenticator_id, "authenticator_id")
        _digest(self.principal_digest, "principal_digest")
        if not isinstance(self.verifier, HumanDecisionVerifier) or not callable(
            getattr(self.verifier, "verify_signed_assertion", None)
        ):
            raise TypeError("verifier must be a HumanDecisionVerifier")
        object.__setattr__(self, "_pid", os.getpid())

    def __copy__(self) -> NoReturn:
        raise TypeError("human decision verifier registration cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("human decision verifier registration cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("human decision verifier registration cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("human decision verifier registration cannot be serialized")

    def __repr__(self) -> str:
        return "<human-decision-verifier-registration identity=redacted>"


class _PinnedHumanDecisionVerifier:
    """Verifier view pinned to one exact trusted-composition identity."""

    __slots__ = ("_key", "_pid", "_verifier")
    _key: tuple[str, str, str]
    _pid: int
    _verifier: HumanDecisionVerifier

    def __init__(
        self,
        *,
        key: tuple[str, str, str],
        verifier: HumanDecisionVerifier,
        pid: int,
    ) -> None:
        object.__setattr__(self, "_key", key)
        object.__setattr__(self, "_verifier", verifier)
        object.__setattr__(self, "_pid", pid)

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("pinned human decision verifier is immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("pinned human decision verifier is immutable")

    def verify_signed_assertion(
        self,
        assertion: SignedHumanDecisionAssertion,
        *,
        signing_bytes: bytes,
    ) -> bool:
        if os.getpid() != self._pid:
            raise HumanDecisionVerifierRegistryProcessMismatch(
                "human decision verifier cannot cross a process boundary"
            )
        if type(assertion) is not SignedHumanDecisionAssertion:
            return False
        if type(signing_bytes) is not bytes:
            return False
        if (
            assertion.audience,
            assertion.authenticator_id,
            assertion.principal_digest,
        ) != self._key:
            return False
        expected_signing_bytes = assertion.signing_bytes()
        if not hmac.compare_digest(signing_bytes, expected_signing_bytes):
            return False
        result = self._verifier.verify_signed_assertion(
            assertion,
            signing_bytes=expected_signing_bytes,
        )
        return result if type(result) is bool else False

    def __copy__(self) -> NoReturn:
        raise TypeError("human decision verifier cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("human decision verifier cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("human decision verifier cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("human decision verifier cannot be serialized")

    def __repr__(self) -> str:
        return "<pinned-human-decision-verifier>"


class TrustedHumanDecisionVerifierRegistry:
    """Immutable process-local registry with exact three-part lookup only."""

    __slots__ = ("_pid", "_verifiers")
    _pid: int
    _verifiers: Mapping[tuple[str, str, str], _PinnedHumanDecisionVerifier]

    def __init__(
        self,
        registrations: Iterable[HumanDecisionVerifierRegistration],
    ) -> None:
        pid = os.getpid()
        copied: dict[tuple[str, str, str], _PinnedHumanDecisionVerifier] = {}
        for registration in registrations:
            if type(registration) is not HumanDecisionVerifierRegistration:
                raise TypeError(
                    "registrations must contain HumanDecisionVerifierRegistration values"
                )
            if registration._pid != pid:
                raise HumanDecisionVerifierRegistryProcessMismatch(
                    "human decision verifier registration cannot cross a process boundary"
                )
            key = (
                registration.audience,
                registration.authenticator_id,
                registration.principal_digest,
            )
            if key in copied:
                raise ValueError("duplicate human decision verifier registration")
            copied[key] = _PinnedHumanDecisionVerifier(
                key=key,
                verifier=registration.verifier,
                pid=pid,
            )
        if not copied:
            raise ValueError("at least one human decision verifier registration is required")
        object.__setattr__(
            self,
            "_verifiers",
            MappingProxyType(copied),
        )
        object.__setattr__(self, "_pid", pid)

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("human decision verifier registry is immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("human decision verifier registry is immutable")

    def resolve(
        self,
        assertion: SignedHumanDecisionAssertion,
    ) -> HumanDecisionVerifier:
        """Resolve only the verifier pinned to the assertion's exact identity."""

        if os.getpid() != self._pid:
            raise HumanDecisionVerifierRegistryProcessMismatch(
                "human decision verifier registry cannot cross a process boundary"
            )
        if type(assertion) is not SignedHumanDecisionAssertion:
            raise TypeError("assertion must be a SignedHumanDecisionAssertion")
        key = (
            assertion.audience,
            assertion.authenticator_id,
            assertion.principal_digest,
        )
        verifier = self._verifiers.get(key)
        if verifier is None:
            raise UnknownHumanDecisionVerifier(
                "no exact human decision verifier is registered for this assertion"
            )
        return verifier

    def __copy__(self) -> NoReturn:
        raise TypeError("human decision verifier registry cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("human decision verifier registry cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("human decision verifier registry cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("human decision verifier registry cannot be serialized")

    def __repr__(self) -> str:
        return f"<trusted-human-decision-verifier-registry count={len(self._verifiers)}>"


__all__ = [
    "MAX_SIGNED_HUMAN_DECISION_ASSERTION_BYTES",
    "SIGNED_HUMAN_DECISION_ASSERTION_SCHEMA",
    "HumanDecisionVerifierRegistration",
    "HumanDecisionVerifierRegistryProcessMismatch",
    "SignedHumanDecisionAssertionCodecError",
    "TrustedHumanDecisionVerifierRegistry",
    "UnknownHumanDecisionVerifier",
    "decode_signed_human_decision_assertion",
    "encode_signed_human_decision_assertion",
]
