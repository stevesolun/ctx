from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPResponse
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from scripts import ctx_ab_deterministic_bridge as bridge


APPROVAL_DIGEST = hashlib.sha256(b"reviewed deterministic bridge config").hexdigest()
REQUEST_BYTES = b'{"model":"ctx-deterministic","messages":[{"role":"user","content":"abcd"}]}'


def _request(
    server: bridge.DeterministicProviderBridge,
    body: bytes = REQUEST_BYTES,
    *,
    approval_digest: str = APPROVAL_DIGEST,
    path: str = "/v1/chat/completions",
    content_type: str = "application/json",
) -> tuple[int, bytes]:
    request = Request(
        f"{server.base_url}{path}",
        data=body,
        method="POST",
        headers={
            "Content-Type": content_type,
            bridge.APPROVAL_DIGEST_HEADER: approval_digest,
        },
    )
    try:
        response: HTTPResponse = urlopen(request, timeout=2)
    except HTTPError as error:
        return error.code, error.read()
    with response:
        return response.status, response.read()


def _raw_request(
    server: bridge.DeterministicProviderBridge,
    request_bytes: bytes,
) -> tuple[int, dict[str, object]]:
    endpoint = urlsplit(server.base_url)
    assert endpoint.hostname is not None
    assert endpoint.port is not None
    with socket.create_connection((endpoint.hostname, endpoint.port), timeout=2) as connection:
        connection.sendall(request_bytes)
        connection.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while chunk := connection.recv(65_536):
            chunks.append(chunk)

    response = b"".join(chunks)
    head, body = response.split(b"\r\n\r\n", 1)
    status = int(head.split(b" ", 2)[1])
    payload = json.loads(body)
    assert isinstance(payload, dict)
    return status, payload


def test_bridge_returns_repeatable_openai_response_and_exact_record() -> None:
    with bridge.DeterministicProviderBridge(
        approval_digest=APPROVAL_DIGEST,
        token_budget=20,
        response_content="done",
    ) as server:
        first_status, first_bytes = _request(server)
        second_status, second_bytes = _request(server)

        records = server.records

    assert (first_status, second_status) == (200, 200)
    assert first_bytes == second_bytes
    response = json.loads(first_bytes)
    assert response["choices"] == [
        {
            "finish_reason": "stop",
            "index": 0,
            "message": {"content": "done", "role": "assistant"},
        }
    ]
    assert response["usage"] == {
        "cached_input_tokens": 0,
        "completion_tokens": 1,
        "input_tokens": 19,
        "output_tokens": 1,
        "prompt_tokens": 19,
        "prompt_tokens_details": {"cached_tokens": 0},
        "total_tokens": 20,
        "uncached_input_tokens": 19,
    }
    assert len(records) == 2
    assert records[0].request_body_bytes == REQUEST_BYTES
    assert records[0].request_body_sha256 == hashlib.sha256(REQUEST_BYTES).hexdigest()
    assert records[0].method == "POST"
    assert records[0].path == "/v1/chat/completions"
    assert records[0].response_status == 200
    assert records[0].approval_digest == APPROVAL_DIGEST
    assert records[0].messages == (bridge.CapturedMessage(role="user", content="abcd"),)
    assert records[0].usage == bridge.TokenUsage(
        input_tokens=19,
        cached_input_tokens=0,
        uncached_input_tokens=19,
        output_tokens=1,
        total_tokens=20,
    )


def test_bridge_rejects_unapproved_or_missing_digest_without_recording() -> None:
    with bridge.DeterministicProviderBridge(
        approval_digest=APPROVAL_DIGEST,
        token_budget=100,
    ) as server:
        mismatch_status, mismatch_body = _request(server, approval_digest="0" * 64)
        missing = Request(
            f"{server.base_url}/v1/chat/completions",
            data=REQUEST_BYTES,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(HTTPError) as missing_error:
            urlopen(missing, timeout=2)
        bearer_mismatch = Request(
            f"{server.base_url}/v1/chat/completions",
            data=REQUEST_BYTES,
            method="POST",
            headers={
                "Authorization": f"Bearer {'0' * 64}",
                "Content-Type": "application/json",
            },
        )
        with pytest.raises(HTTPError) as bearer_mismatch_error:
            urlopen(bearer_mismatch, timeout=2)
        records = server.records

    assert mismatch_status == 403
    assert json.loads(mismatch_body)["error"]["code"] == "approval_digest_mismatch"
    assert missing_error.value.code == 403
    assert bearer_mismatch_error.value.code == 403
    assert records == ()


def test_bridge_accepts_approval_digest_as_openai_bearer_api_key() -> None:
    with bridge.DeterministicProviderBridge(
        approval_digest=APPROVAL_DIGEST,
        token_budget=100,
    ) as server:
        request = Request(
            f"{server.base_url}/v1/chat/completions",
            data=REQUEST_BYTES,
            method="POST",
            headers={
                "Authorization": f"Bearer {APPROVAL_DIGEST}",
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=2) as response:
            status = response.status
        records = server.records

    assert status == 200
    assert len(records) == 1
    assert records[0].approval_digest == APPROVAL_DIGEST


def test_bridge_enforces_configured_and_request_output_token_budgets() -> None:
    with bridge.DeterministicProviderBridge(
        approval_digest=APPROVAL_DIGEST,
        token_budget=19,
        response_content="done",
    ) as server:
        status, body = _request(server)
        configured_records = server.records

    output_limited = (
        b'{"model":"ctx-deterministic","max_tokens":1,"messages":'
        b'[{"role":"user","content":"abcd"}]}'
    )
    with bridge.DeterministicProviderBridge(
        approval_digest=APPROVAL_DIGEST,
        token_budget=100,
        response_content="12345",
    ) as server:
        limited_status, limited_body = _request(server, output_limited)
        limited_records = server.records

    assert status == 413
    assert json.loads(body)["error"]["code"] == "token_budget_exceeded"
    assert configured_records == ()
    assert limited_status == 413
    assert json.loads(limited_body)["error"]["code"] == "request_output_budget_exceeded"
    assert limited_records == ()


@pytest.mark.parametrize(
    ("body", "content_type", "error_code"),
    [
        (b"{", "application/json", "malformed_json"),
        (
            b'{"model":"one","model":"two","messages":[]}',
            "application/json",
            "malformed_json",
        ),
        (b"[]", "application/json", "invalid_request"),
        (
            b'{"model":"ctx-deterministic","messages":[],"stream":true}',
            "application/json",
            "streaming_not_supported",
        ),
        (REQUEST_BYTES, "text/plain", "unsupported_media_type"),
    ],
)
def test_bridge_rejects_malformed_requests(
    body: bytes,
    content_type: str,
    error_code: str,
) -> None:
    with bridge.DeterministicProviderBridge(
        approval_digest=APPROVAL_DIGEST,
        token_budget=100,
    ) as server:
        status, response_body = _request(server, body, content_type=content_type)

    assert status in {400, 415, 422}
    assert json.loads(response_body)["error"]["code"] == error_code


def test_bridge_rejects_unknown_paths_and_non_loopback_bind() -> None:
    with pytest.raises(ValueError, match="loopback"):
        bridge.DeterministicProviderBridge(
            approval_digest=APPROVAL_DIGEST,
            token_budget=100,
            bind_host="0.0.0.0",
        )

    with bridge.DeterministicProviderBridge(
        approval_digest=APPROVAL_DIGEST,
        token_budget=100,
    ) as server:
        status, response_body = _request(server, path="/not-an-api")

    assert status == 404
    assert json.loads(response_body)["error"]["code"] == "unknown_path"


@pytest.mark.parametrize(
    "request_target",
    [
        b"http://[invalid/v1/chat/completions",
        b"http://127.0.0.1/v1/chat/completions",
        b"//127.0.0.1/v1/chat/completions",
    ],
    ids=["invalid-absolute-uri", "absolute-form", "network-form"],
)
def test_bridge_accepts_only_exact_origin_form_request_target(request_target: bytes) -> None:
    with bridge.DeterministicProviderBridge(
        approval_digest=APPROVAL_DIGEST,
        token_budget=100,
    ) as server:
        status, response = _raw_request(
            server,
            b"POST "
            + request_target
            + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            + bridge.APPROVAL_DIGEST_HEADER.encode("ascii")
            + b": "
            + APPROVAL_DIGEST.encode("ascii")
            + b"\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(REQUEST_BYTES)).encode("ascii")
            + b"\r\n\r\n"
            + REQUEST_BYTES,
        )

    assert status in {400, 404}
    assert response["error"]["code"] in {  # type: ignore[index]
        "invalid_request_target",
        "unknown_path",
    }


@pytest.mark.parametrize(
    "content_length",
    [b"\xff", b"9" * 5_000],
    ids=["non-ascii", "absurd-decimal-length"],
)
def test_bridge_bounds_pathological_content_length(content_length: bytes) -> None:
    with bridge.DeterministicProviderBridge(
        approval_digest=APPROVAL_DIGEST,
        token_budget=100,
    ) as server:
        status, response = _raw_request(
            server,
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            + bridge.APPROVAL_DIGEST_HEADER.encode("ascii")
            + b": "
            + APPROVAL_DIGEST.encode("ascii")
            + b"\r\nContent-Type: application/json\r\nContent-Length: "
            + content_length
            + b"\r\n\r\n",
        )

    assert status == 400
    assert response["error"]["code"] == "invalid_content_length"  # type: ignore[index]


@pytest.mark.parametrize("header_name", [bridge.APPROVAL_DIGEST_HEADER, "Authorization"])
def test_bridge_bounds_non_ascii_approval_headers(header_name: str) -> None:
    header_value = b"Bearer \xff" if header_name == "Authorization" else b"\xff"
    with bridge.DeterministicProviderBridge(
        approval_digest=APPROVAL_DIGEST,
        token_budget=100,
    ) as server:
        status, response = _raw_request(
            server,
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            + header_name.encode("ascii")
            + b": "
            + header_value
            + b"\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(REQUEST_BYTES)).encode("ascii")
            + b"\r\n\r\n"
            + REQUEST_BYTES,
        )

    assert status == 403
    assert response["error"]["code"] == "approval_digest_mismatch"  # type: ignore[index]


def test_bridge_catch_all_returns_bounded_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with bridge.DeterministicProviderBridge(
        approval_digest=APPROVAL_DIGEST,
        token_budget=100,
    ) as server:
        monkeypatch.setattr(
            server,
            "_handle_post",
            lambda _handler: (_ for _ in ()).throw(RuntimeError("private details")),
        )
        status, response = _raw_request(
            server,
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\n\r\n",
        )

    assert status == 400
    assert response == {
        "error": {
            "code": "malformed_request",
            "message": "request could not be processed",
            "type": "invalid_request_error",
        }
    }


def test_bridge_records_parallel_requests_without_losing_evidence() -> None:
    with bridge.DeterministicProviderBridge(
        approval_digest=APPROVAL_DIGEST,
        token_budget=100,
    ) as server:
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = tuple(executor.map(lambda _: _request(server), range(32)))
        records = server.records

    assert {status for status, _ in results} == {200}
    assert len(records) == 32
    assert {record.request_body_sha256 for record in records} == {
        hashlib.sha256(REQUEST_BYTES).hexdigest()
    }


def test_bridge_is_terminally_closed_and_does_not_mix_lifetimes() -> None:
    server = bridge.DeterministicProviderBridge(
        approval_digest=APPROVAL_DIGEST,
        token_budget=100,
    ).start()
    assert _request(server)[0] == 200

    server.close()
    server.close()

    assert len(server.records) == 1
    with pytest.raises(RuntimeError, match="closed"):
        server.start()
    with pytest.raises(RuntimeError, match="not running"):
        _ = server.base_url


def test_start_failure_closes_bound_socket_and_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = bridge.DeterministicProviderBridge(
        approval_digest=APPROVAL_DIGEST,
        token_budget=100,
    )
    bound_ports: list[int] = []

    def fail_start(_thread: threading.Thread) -> None:
        underlying = server._server
        assert underlying is not None
        bound_ports.append(underlying.server_port)
        raise RuntimeError("injected start failure")

    monkeypatch.setattr(bridge.threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="injected start failure"):
        server.start()

    assert len(bound_ports) == 1
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", bound_ports[0]))
    with pytest.raises(RuntimeError, match="closed"):
        server.start()
    with pytest.raises(RuntimeError, match="not running"):
        _ = server.base_url
    server.close()


def test_bridge_rejects_start_while_close_is_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = bridge.DeterministicProviderBridge(
        approval_digest=APPROVAL_DIGEST,
        token_budget=100,
    ).start()
    underlying = server._server
    assert underlying is not None
    shutdown_entered = threading.Event()
    allow_shutdown = threading.Event()
    original_shutdown = underlying.shutdown

    def blocking_shutdown() -> None:
        shutdown_entered.set()
        assert allow_shutdown.wait(timeout=2)
        original_shutdown()

    monkeypatch.setattr(underlying, "shutdown", blocking_shutdown)
    close_thread = threading.Thread(target=server.close)
    close_thread.start()
    assert shutdown_entered.wait(timeout=2)
    try:
        with pytest.raises(RuntimeError, match="closing"):
            server.start()
    finally:
        allow_shutdown.set()
        close_thread.join(timeout=3)

    assert not close_thread.is_alive()
    with pytest.raises(RuntimeError, match="closed"):
        server.start()


def test_close_terminates_partial_request_before_freezing_records() -> None:
    server = bridge.DeterministicProviderBridge(
        approval_digest=APPROVAL_DIGEST,
        token_budget=100,
    ).start()
    endpoint = urlsplit(server.base_url)
    assert endpoint.hostname is not None
    assert endpoint.port is not None
    connection = socket.create_connection((endpoint.hostname, endpoint.port), timeout=2)
    partial_body = REQUEST_BYTES[:10]
    connection.sendall(
        b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        + bridge.APPROVAL_DIGEST_HEADER.encode("ascii")
        + b": "
        + APPROVAL_DIGEST.encode("ascii")
        + b"\r\nContent-Type: application/json\r\nContent-Length: "
        + str(len(REQUEST_BYTES)).encode("ascii")
        + b"\r\n\r\n"
        + partial_body
    )
    time.sleep(0.05)

    close_started = time.monotonic()
    server.close()
    close_elapsed = time.monotonic() - close_started
    frozen_records = server.records
    try:
        connection.sendall(REQUEST_BYTES[len(partial_body) :])
    except OSError:
        pass
    connection.settimeout(0.2)
    received = b""
    try:
        while chunk := connection.recv(65_536):
            received += chunk
    except OSError:
        pass
    finally:
        connection.close()
    time.sleep(0.05)

    assert close_elapsed < 2
    assert b" 200 " not in received
    assert frozen_records == ()
    assert server.records == frozen_records


@pytest.mark.parametrize("bad_digest", ["", "A" * 64, "x" * 64, "0" * 63])
def test_bridge_rejects_invalid_config_digest(bad_digest: str) -> None:
    with pytest.raises(ValueError, match="approval_digest"):
        bridge.DeterministicProviderBridge(
            approval_digest=bad_digest,
            token_budget=100,
        )
