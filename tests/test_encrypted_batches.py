import base64
import gzip
import json
import os
from functools import partial

import pytest
from joserfc import jwe

from app.api import uploads
from app.core.encryption import EncryptionKeys, InvalidEnvelope
from app.db.mongo import PersistenceError
from app.services import batch_codec, upload_pipeline

URL = "/api/v1/traffic-log-uploads"
HEADERS = {"Content-Type": "application/jose"}
GZIP_HEADERS = {"Content-Type": "application/json", "Content-Encoding": "gzip"}


def encrypt(keys, compressed, kid="current", **headers):
    return jwe.encrypt_compact(
        {
            "alg": keys.algorithm,
            "enc": keys.encryption,
            "kid": kid,
            "cty": "application/gzip",
            **headers,
        },
        compressed,
        keys.keys[kid],
        registry=keys.registry,
    )


@pytest.mark.parametrize("kid", ["current", "previous"])
def test_encrypted_single_log_is_decrypted_validated_and_redacted(
    encrypted_client, repository, rich_traffic_payload, encryption_keys, kid, drain_uploads
):
    repository.get_device_configuration.return_value = {"version": 1, "encryption_mode": "required"}
    body = gzip.compress(json.dumps({"logs": [rich_traffic_payload]}).encode())
    response = encrypted_client.post(
        URL, content=encrypt(encryption_keys, body, kid), headers=HEADERS
    )
    assert response.status_code == 202
    drain_uploads()
    assert (
        encrypted_client.get(f"{URL}/{response.json()['upload_id']}").json()["accepted_count"] == 1
    )
    stored = repository.insert_queued_traffic_logs.call_args.args[0][0]
    assert stored["logdata"]["request_headers"]["Authorization"] == "[ENCRYPTED]"
    assert stored["logdata"]["response_headers"]["Set-Cookie"] == "[ENCRYPTED]"
    assert stored["logdata"]["destination_ip"] == rich_traffic_payload["destination_ip"]
    assert stored["logdata"]["response_size_bytes"] == rich_traffic_payload["response_size_bytes"]
    assert stored["logdata"]["process_name"] == rich_traffic_payload["process_name"]
    assert "body" not in stored["logdata"]
    assert "ciphertext" not in stored


def test_required_encryption_uses_the_authenticated_device_for_the_whole_batch(
    encrypted_client, repository, traffic_payload
):
    repository.get_device_configuration.side_effect = lambda device: {
        "version": 1,
        "encryption_mode": "required" if device == "device-456" else "optional",
    }
    body = gzip.compress(json.dumps({"logs": [traffic_payload, traffic_payload]}).encode())
    response = encrypted_client.post(URL, content=body, headers=GZIP_HEADERS)
    assert response.status_code == 403
    repository.get_device_configuration.assert_awaited_once_with("device-456")
    repository.insert_queued_traffic_logs.assert_not_awaited()


def test_optional_policy_allows_both_formats(
    encrypted_client, repository, traffic_payload, encryption_keys
):
    repository.get_device_configuration.return_value = {"version": 1, "encryption_mode": "optional"}
    body = gzip.compress(json.dumps({"logs": [traffic_payload]}).encode())
    assert encrypted_client.post(URL, content=body, headers=GZIP_HEADERS).status_code == 202
    assert (
        encrypted_client.post(
            URL, content=encrypt(encryption_keys, body), headers=HEADERS
        ).status_code
        == 202
    )


def test_tls_only_device_rejects_application_encryption(
    encrypted_client, repository, traffic_payload, encryption_keys
):
    body = gzip.compress(json.dumps({"logs": [traffic_payload]}).encode())
    response = encrypted_client.post(URL, content=encrypt(encryption_keys, body), headers=HEADERS)
    assert response.status_code == 403
    repository.insert_queued_traffic_logs.assert_not_awaited()


def test_invalid_envelopes_and_tampering_do_not_publish(
    encrypted_client, repository, encryption_keys, drain_uploads
):
    repository.get_device_configuration.return_value = {"version": 1, "encryption_mode": "optional"}
    value = encrypt(encryption_keys, gzip.compress(b'{"logs": []}'))
    parts = value.split(".")
    parts[3] = ("A" if parts[3][0] != "A" else "B") + parts[3][1:]
    for body in ["malformed", ".".join(parts), value[:-4], "W10...."]:
        response = encrypted_client.post(URL, content=body, headers=HEADERS)
        assert response.status_code == 202
        drain_uploads()
        assert (
            encrypted_client.get(f"{URL}/{response.json()['upload_id']}").json()["error_code"]
            == "invalid_content"
        )
    repository.insert_queued_traffic_logs.assert_not_awaited()


def test_algorithm_confusion_zip_and_unknown_keys_are_rejected_before_decryption(encryption_keys):
    body = gzip.compress(b"{}")
    value = encrypt(encryption_keys, body, cty="application/json")
    with pytest.raises(InvalidEnvelope):
        encryption_keys.decrypt(value.encode())
    value = encrypt(encryption_keys, body)
    without_previous = EncryptionKeys({"previous": encryption_keys.keys["previous"]}, "previous")
    with pytest.raises(InvalidEnvelope):
        without_previous.decrypt(value.encode())
    original_header = jwe.extract_compact(value.encode(), encryption_keys.registry).protected
    for changes in (
        {"alg": "RSA-OAEP-256"},
        {"enc": "A128CBC-HS256"},
        {"kid": ["current"]},
        {"zip": "DEF"},
        {"jku": "https://example.test"},
        {"epk": {**original_header["epk"], "d": "private"}},
        {"epk": {**original_header["epk"], "crv": "P-256"}},
        {"epk": None},
    ):
        parts = value.split(".")
        header = {**original_header, **changes}
        parts[0] = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
        with pytest.raises(InvalidEnvelope):
            encryption_keys.decrypt(".".join(parts).encode())


def test_large_encrypted_payload_is_not_limited_to_library_default(encryption_keys):
    body = gzip.compress(os.urandom(100_000))
    value = encrypt(encryption_keys, body)
    assert encryption_keys.decrypt(value.encode()) == body


@pytest.mark.parametrize(
    "limit", ["MAX_ENCRYPTED_BYTES", "MAX_COMPRESSED_BYTES", "MAX_DECOMPRESSED_BYTES"]
)
def test_encrypted_limits_are_enforced(
    encrypted_client,
    repository,
    traffic_payload,
    encryption_keys,
    monkeypatch,
    limit,
    drain_uploads,
):
    repository.get_device_configuration.return_value = {"version": 1, "encryption_mode": "optional"}
    body = encrypt(encryption_keys, gzip.compress(json.dumps({"logs": [traffic_payload]}).encode()))
    if limit == "MAX_ENCRYPTED_BYTES":
        monkeypatch.setattr(uploads, limit, 32)
    else:
        keyword = (
            "max_compressed_bytes" if limit == "MAX_COMPRESSED_BYTES" else "max_decompressed_bytes"
        )
        monkeypatch.setattr(
            upload_pipeline,
            "parse_compressed_batch",
            partial(batch_codec.parse_compressed_batch, **{keyword: 32}),
        )
    response = encrypted_client.post(URL, content=body, headers=HEADERS)
    assert response.status_code == (413 if limit == "MAX_ENCRYPTED_BYTES" else 202)
    if response.status_code == 202:
        drain_uploads()
        assert (
            encrypted_client.get(f"{URL}/{response.json()['upload_id']}").json()["error_code"]
            == "invalid_content"
        )
    repository.insert_queued_traffic_logs.assert_not_awaited()


def test_encrypted_batches_reject_client_attribution_and_enforce_gzip_contract(
    encrypted_client, repository, traffic_payload, encryption_keys, drain_uploads
):
    repository.get_device_configuration.return_value = {"version": 1, "encryption_mode": "required"}
    forged_attribution = {**traffic_payload, "user_id": "other"}
    body = gzip.compress(json.dumps({"logs": [forged_attribution]}).encode())
    for compressed in [body, b"not gzip", gzip.compress(b"{}") + gzip.compress(b"{}")]:
        response = encrypted_client.post(
            URL, content=encrypt(encryption_keys, compressed), headers=HEADERS
        )
        assert response.status_code == 202
        drain_uploads()
        assert (
            encrypted_client.get(f"{URL}/{response.json()['upload_id']}").json()["error_code"]
            == "invalid_content"
        )
    repository.insert_queued_traffic_logs.assert_not_awaited()


def test_encrypted_media_type_has_no_outer_gzip(encrypted_client, repository):
    response = encrypted_client.post(
        URL, content=b"invalid", headers={**HEADERS, "Content-Encoding": "gzip"}
    )
    assert response.status_code == 415
    repository.insert_queued_traffic_logs.assert_not_awaited()


def test_missing_parser_keys_fail_asynchronously(
    authorized_client, repository, encryption_keys, traffic_payload, drain_uploads
):
    repository.get_device_configuration.return_value = {"version": 1, "encryption_mode": "optional"}
    body = encrypt(encryption_keys, gzip.compress(json.dumps({"logs": [traffic_payload]}).encode()))
    response = authorized_client.post(URL, content=body, headers=HEADERS)
    assert response.status_code == 202
    drain_uploads()
    assert (
        authorized_client.get(f"{URL}/{response.json()['upload_id']}").json()["error_code"]
        == "invalid_content"
    )
    repository.insert_queued_traffic_logs.assert_not_awaited()


def test_policy_failure_does_not_fall_back_to_plaintext(
    authorized_client, repository, traffic_payload
):
    repository.get_device_configuration.side_effect = PersistenceError("private details")
    body = gzip.compress(json.dumps({"logs": [traffic_payload]}).encode())
    response = authorized_client.post(URL, content=body, headers=GZIP_HEADERS)
    assert response.status_code == 503
    assert "private details" not in response.text
    repository.insert_queued_traffic_logs.assert_not_awaited()
