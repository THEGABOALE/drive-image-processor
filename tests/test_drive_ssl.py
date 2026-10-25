"""
GD03B-R2: clasificación de los errores TLS/SSL.

Comprobado con las versiones instaladas (httplib2 0.32.0,
google-api-python-client 2.201.0, google-auth-httplib2 0.4.4):

- httplib2 0.32 ya no tiene SSLHandshakeError: los fallos de TLS
  llegan como ssl.SSLError o ssl.SSLCertVerificationError sin envolver
  (HTTPSConnectionWithTimeout.connect los vuelve a lanzar).
- googleapiclient (_retry_request) los relanza tal cual con
  num_retries=0, que es como se usan aquí.
- google-auth envuelve los fallos de transporte en TransportError
  (implícita o explícitamente encadenados), y requests los anida en
  urllib3 (MaxRetryError.reason).

Un certificado inválido, en cualquier punto de la cadena, no se
reintenta. Los cortes de una conexión TLS ya establecida (EOF) siguen
reintentándose.
"""

import socket
import ssl

import httplib2
import pytest
import requests
import urllib3
from google.auth.credentials import AnonymousCredentials
from google.auth.exceptions import TransportError
from googleapiclient.http import HttpRequest

from image_processor.google_drive import client, uploads
from image_processor.google_drive.errors import (
    DriveCertificateError,
    DriveNetworkError,
    DriveSecureConnectionError,
)


SECRET = "token=ya29.SECRETO-EN-EL-MENSAJE"


def certificate_error():
    return ssl.SSLCertVerificationError(
        1, f"[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed ({SECRET})"
    )


def transport_from(error):
    """Como google.auth.transport.requests: raise TransportError(e) from e."""

    try:
        raise error
    except Exception as exc:
        try:
            raise TransportError(exc) from exc
        except TransportError as wrapped:
            return wrapped


def transport_in_context(error):
    """Como google_auth_httplib2: raise TransportError(...) dentro del except."""

    try:
        raise error
    except Exception:
        try:
            raise TransportError(f"fallo de transporte ({SECRET})")
        except TransportError as wrapped:
            return wrapped


def requests_chain(error):
    """requests.SSLError -> urllib3 MaxRetryError -> urllib3 SSLError -> ssl."""

    reason = urllib3.exceptions.SSLError(error)
    retry = urllib3.exceptions.MaxRetryError(None, "https://oauth2.googleapis.com/token", reason)
    return transport_from(requests.exceptions.SSLError(retry))


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(certificate_error, id="directo"),
        pytest.param(lambda: ssl.CertificateError(f"hostname mismatch {SECRET}"), id="nombre"),
        pytest.param(lambda: transport_from(certificate_error()), id="transport-causa"),
        pytest.param(lambda: transport_in_context(certificate_error()), id="transport-contexto"),
        pytest.param(lambda: TransportError(certificate_error()), id="transport-argumento"),
        pytest.param(lambda: requests_chain(certificate_error()), id="requests-urllib3"),
        pytest.param(
            lambda: ssl.SSLError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] verify failed"),
            id="sslerror-con-motivo",
        ),
    ],
)
def test_certificate_errors_are_never_retried(build):
    translated = client.translate_error(build())

    assert isinstance(translated.error, DriveCertificateError)
    assert translated.retryable is False
    assert "no se desactiva" in str(translated.error)
    assert SECRET not in str(translated.error)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: ssl.SSLError(1, f"[SSL: WRONG_VERSION_NUMBER] wrong version {SECRET}"),
            id="version",
        ),
        pytest.param(
            lambda: ssl.SSLError(1, "[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] handshake"),
            id="negociacion",
        ),
        pytest.param(
            lambda: transport_from(ssl.SSLError(1, "[SSL: TLSV1_ALERT_PROTOCOL_VERSION]")),
            id="encadenado",
        ),
    ],
)
def test_tls_handshake_failures_are_not_retried(build):
    translated = client.translate_error(build())

    assert isinstance(translated.error, DriveSecureConnectionError)
    assert not isinstance(translated.error, DriveCertificateError)
    assert translated.retryable is False
    assert "no se desactiva" in str(translated.error)
    assert SECRET not in str(translated.error)


def test_legacy_httplib2_handshake_error_is_not_retried(monkeypatch):
    # Versiones anteriores de httplib2 tenían SSLHandshakeError; la
    # instalada no, pero la clasificación la reconoce si existe.
    class SSLHandshakeError(httplib2.HttpLib2Error):
        pass

    monkeypatch.setattr(httplib2, "SSLHandshakeError", SSLHandshakeError, raising=False)

    translated = client.translate_error(SSLHandshakeError(f"handshake {SECRET}"))

    assert isinstance(translated.error, DriveSecureConnectionError)
    assert translated.retryable is False
    assert SECRET not in str(translated.error)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: ssl.SSLEOFError(8, "EOF occurred in violation of protocol"), id="tls-eof"),
        pytest.param(lambda: ssl.SSLZeroReturnError(6, "TLS/SSL connection has been closed"), id="tls-cerrada"),
        pytest.param(ConnectionResetError, id="reset"),
        pytest.param(TimeoutError, id="timeout"),
        pytest.param(socket.timeout, id="socket-timeout"),
        pytest.param(lambda: httplib2.ServerNotFoundError("sin DNS"), id="dns"),
        pytest.param(lambda: TransportError("sin conexión"), id="transport"),
        pytest.param(lambda: transport_from(ConnectionResetError()), id="transport-reset"),
    ],
)
def test_temporary_connection_errors_are_still_retried(build):
    translated = client.translate_error(build())

    assert isinstance(translated.error, DriveNetworkError)
    assert not isinstance(translated.error, DriveSecureConnectionError)
    assert translated.retryable is True


class RaisingHttp:
    """Transporte httplib2 que falla como la conexión real."""

    def __init__(self, error):
        self.error = error
        self.calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        raise self.error


def api_request(http):
    return HttpRequest(
        http, lambda resp, content: content, "https://www.googleapis.com/drive/v3/about"
    )


def test_real_api_request_with_invalid_certificate_is_tried_once():
    http = RaisingHttp(certificate_error())
    sleeps = []

    with pytest.raises(DriveCertificateError):
        client.execute(api_request(http), sleep=sleeps.append)

    assert http.calls == 1
    assert sleeps == []


def test_real_api_request_with_tls_eof_is_retried():
    http = RaisingHttp(ssl.SSLEOFError(8, "EOF"))
    sleeps = []

    with pytest.raises(DriveNetworkError):
        client.execute(api_request(http), sleep=sleeps.append)

    assert http.calls == client.MAX_ATTEMPTS
    assert len(sleeps) == client.MAX_ATTEMPTS - 1


def test_real_resumable_upload_with_invalid_certificate():
    service = client.build_service(AnonymousCredentials())
    request = uploads._create_request(service, {"name": "x.png"}, b"datos", chunk_size=256 * 1024)
    request.http = RaisingHttp(certificate_error())

    with pytest.raises(ssl.SSLCertVerificationError) as raised:
        request.next_chunk(num_retries=0)

    translated = client.translate_error(raised.value)
    assert isinstance(translated.error, DriveCertificateError)
    assert translated.retryable is False
