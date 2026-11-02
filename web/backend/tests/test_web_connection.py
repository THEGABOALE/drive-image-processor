import json
import logging
import threading

import pytest
from conftest import (
    DESKTOP_CLIENT_ID,
    DESKTOP_SECRET,
    FakeCredentials,
    FakeFlow,
    MemoryTokenStore,
    READ_GRANTED,
    WRITE_GRANTED,
    read_credentials,
    write_credentials,
)
from image_processor.google_drive import auth
from image_processor.google_drive.errors import DriveAuthError
from oauthlib.oauth2.rfc6749.errors import AccessDeniedError

from web.backend.app.connection import (
    ConnectionManager,
    IdentityError,
    NotConnectedError,
    OPENID_SCOPE,
    State,
    verify_google_id_token,
)
from web.backend.app.token_store import TokenStoreError


# Estado de autenticación ----------------------------------------------------

def test_starts_disconnected_without_stored_tokens(make_manager):
    status = make_manager().status()

    assert status.state is State.DISCONNECTED
    assert status.account is None


def test_status_after_connecting_shows_the_account(connected):
    status = connected.status()

    assert status.state is State.CONNECTED
    assert status.account.sub == "sub-a"
    assert status.account.email == "prueba@example.com"
    assert status.account.display_name == "Cuenta de prueba"


def test_connection_survives_a_restart(make_manager):
    store = MemoryTokenStore()
    make_manager(read_credentials(), write_credentials(), store=store).start()

    restarted = make_manager(store=store)

    assert restarted.status().state is State.CONNECTED
    assert restarted.status().account.sub == "sub-a"
    assert restarted.script.requests == []


def test_damaged_store_requires_reconnecting(make_manager):
    manager = make_manager(store=MemoryTokenStore(load_error=TokenStoreError("dañado")))

    assert manager.status().state is State.RECONNECT_REQUIRED
    assert manager.status().account is None


def test_connection_of_another_client_is_not_reused(make_manager):
    store = MemoryTokenStore()
    make_manager(read_credentials(), write_credentials(), store=store).start()
    store.data["client_id"] = "otro-proyecto.apps.googleusercontent.com"

    manager = make_manager(store=store)

    assert manager.status().state is State.RECONNECT_REQUIRED
    with pytest.raises(NotConnectedError):
        manager.services()


# Autorización --------------------------------------------------------------

def test_requests_separate_authorizations_with_the_engine_scopes(make_manager, settings):
    manager = make_manager(read_credentials(), write_credentials())
    manager.start()

    read, write = manager.script.requests

    assert set(read["scopes"]) == {*auth.READ.scopes, OPENID_SCOPE}
    assert set(write["scopes"]) == {*auth.WRITE.scopes, OPENID_SCOPE}
    assert read["file"] == str(settings.desktop_client_file)
    assert write["file"] == str(settings.desktop_client_file)


def test_scopes_never_include_full_drive_or_email(make_manager):
    manager = make_manager(read_credentials(), write_credentials())
    manager.start()

    requested = {s for r in manager.script.requests for s in r["scopes"]}

    assert requested == {
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.file",
        "openid",
    }


def test_write_authorization_hints_the_account_of_the_first(make_manager):
    manager = make_manager(read_credentials(), write_credentials())
    manager.start()

    assert manager.script.requests[0]["login_hint"] is None
    assert manager.script.requests[1]["login_hint"] == "sub-a"


def test_opens_the_system_browser_with_a_loopback_redirect(make_manager):
    first = FakeFlow(read_credentials())
    manager = make_manager(first, write_credentials())
    manager.start()

    assert first.kwargs["port"] == 0
    assert first.kwargs["open_browser"] is True
    assert first.kwargs["timeout_seconds"] > 0


def test_services_use_separate_credentials_of_the_desktop_client(connected, drive):
    read, write = connected.services()

    assert read is drive.read
    assert write is drive.write


def test_built_credentials_keep_scopes_apart(settings):
    store = MemoryTokenStore()
    built = []

    def factory(credentials):
        built.append(credentials)
        return credentials

    from conftest import FlowScript, fake_verify_id_token
    from drive_fakes import FakeDrive

    connect_drive = FakeDrive()
    manager = ConnectionManager(
        settings,
        store,
        flow_factory=FlowScript(read_credentials(), write_credentials()),
        verify_id_token=fake_verify_id_token,
        service_factory=lambda c: connect_drive if c.refresh_token == "refresh-lectura" else factory(c),
        start_thread=lambda target: target(),
    )
    manager.start()
    built.clear()

    manager_again = ConnectionManager(
        settings, store, service_factory=factory, start_thread=lambda t: t()
    )
    read, write = manager_again.services()

    assert read.refresh_token == "refresh-lectura"
    assert set(read.scopes) == set(READ_GRANTED)
    assert write.refresh_token == "refresh-escritura"
    assert set(write.scopes) == set(WRITE_GRANTED)
    assert read.client_id == DESKTOP_CLIENT_ID
    assert write.client_id == DESKTOP_CLIENT_ID
    assert read.client_secret == DESKTOP_SECRET


def test_second_start_while_connected_is_rejected(connected):
    with pytest.raises(ValueError):
        connected.start()


def test_real_flow_builds_the_authorization_url_of_the_desktop_client(settings):
    from urllib.parse import parse_qs, urlsplit

    from web.backend.app.connection import default_flow_factory

    flow = default_flow_factory(
        str(settings.desktop_client_file), list(READ_GRANTED), "sub-a"
    )
    flow.redirect_uri = "http://localhost:1234/"
    url, _ = flow.authorization_url()
    params = parse_qs(urlsplit(url).query)

    assert params["client_id"] == [DESKTOP_CLIENT_ID]
    assert params["login_hint"] == ["sub-a"]
    assert params["access_type"] == ["offline"]
    assert set(params["scope"][0].split()) == set(READ_GRANTED)
    assert "include_granted_scopes" not in params

    flow = default_flow_factory(str(settings.desktop_client_file), list(READ_GRANTED))
    flow.redirect_uri = "http://localhost:1234/"
    assert "login_hint" not in flow.authorization_url()[0]


# Cancelación y rechazo -----------------------------------------------------

def test_denied_authorization_saves_nothing(make_manager):
    store = MemoryTokenStore()
    manager = make_manager(AccessDeniedError(), store=store)
    manager.start()

    status = manager.status()
    assert status.state is State.DISCONNECTED
    assert "cancel" in status.message.lower() or "rechaz" in status.message.lower()
    assert store.data is None
    assert store.saves == 0


def test_write_denied_after_read_saves_nothing(make_manager):
    store = MemoryTokenStore()
    manager = make_manager(read_credentials(), AccessDeniedError(), store=store)
    manager.start()

    assert manager.status().state is State.DISCONNECTED
    assert store.saves == 0


def test_timeout_in_the_browser_saves_nothing(make_manager):
    from google_auth_oauthlib.flow import WSGITimeoutError

    store = MemoryTokenStore()
    manager = make_manager(WSGITimeoutError(), store=store)
    manager.start()

    assert manager.status().state is State.DISCONNECTED
    assert manager.status().message
    assert store.saves == 0


def test_cancel_discards_an_authorization_in_progress(make_manager):
    store = MemoryTokenStore()
    holder = {}
    flow = FakeFlow(read_credentials(), on_run=lambda: holder["manager"].cancel())
    manager = make_manager(flow, write_credentials(), store=store)
    holder["manager"] = manager

    manager.start()

    assert manager.status().state is State.DISCONNECTED
    # No se abrió la segunda autorización ni se guardó nada.
    assert len(manager.script.requests) == 1
    assert store.saves == 0


def test_cancel_during_write_discards_both(make_manager):
    store = MemoryTokenStore()
    holder = {}
    flow = FakeFlow(write_credentials(), on_run=lambda: holder["manager"].cancel())
    manager = make_manager(read_credentials(), flow, store=store)
    holder["manager"] = manager

    manager.start()

    assert manager.status().state is State.DISCONNECTED
    assert store.saves == 0


def test_cancel_just_before_saving_discards_the_attempt(make_manager):
    store = MemoryTokenStore()
    manager = make_manager(read_credentials(), write_credentials(), store=store)
    original = manager._service_factory

    def cancel_while_reading_display_data(credentials):
        manager.cancel()
        return original(credentials)

    manager._service_factory = cancel_while_reading_display_data

    manager.start()

    assert manager.status().state is State.DISCONNECTED
    assert store.saves == 0
    assert store.data is None


def test_can_retry_after_cancel_even_if_the_old_attempt_is_still_waiting(make_manager):
    store = MemoryTokenStore()
    release = threading.Event()
    waiting = threading.Event()

    def wait_for_browser():
        waiting.set()
        release.wait(5)

    threads = []

    def start_thread(target):
        thread = threading.Thread(target=target)
        threads.append(thread)
        thread.start()

    manager = make_manager(
        FakeFlow(read_credentials("sub-vieja", "viejo"), on_run=wait_for_browser),
        read_credentials(),
        write_credentials(),
        store=store,
        start_thread=start_thread,
    )

    manager.start()
    assert waiting.wait(5)
    assert manager.status().state is State.AUTHORIZING_READ

    manager.cancel()
    assert manager.status().state is State.DISCONNECTED

    manager.start()
    threads[1].join(5)
    assert manager.status().state is State.CONNECTED
    assert manager.status().account.sub == "sub-a"

    # El intento anterior termina tarde: se descarta sin tocar nada.
    saves = store.saves
    release.set()
    threads[0].join(5)

    assert manager.status().account.sub == "sub-a"
    assert store.saves == saves
    assert len(manager.script.requests) == 3


def test_cancel_when_idle_is_harmless(make_manager):
    manager = make_manager()
    manager.cancel()

    assert manager.status().state is State.DISCONNECTED


# Permisos insuficientes ----------------------------------------------------

@pytest.mark.parametrize(
    "granted",
    [
        ("openid",),
        ("https://www.googleapis.com/auth/drive.readonly",),
        # Permisos más amplios: tampoco se aceptan.
        ("https://www.googleapis.com/auth/drive", "openid"),
        (*READ_GRANTED, "https://www.googleapis.com/auth/drive"),
        (*READ_GRANTED, "email"),
    ],
)
def test_read_authorization_with_other_scopes_is_rejected(make_manager, granted):
    store = MemoryTokenStore()
    manager = make_manager(FakeCredentials(granted, "sub-a", "r"), store=store)
    manager.start()

    assert manager.status().state is State.DISCONNECTED
    assert store.saves == 0
    assert len(manager.script.requests) == 1


@pytest.mark.parametrize(
    "granted",
    [
        ("openid",),
        ("https://www.googleapis.com/auth/drive.file",),
        ("https://www.googleapis.com/auth/drive", "openid"),
        (*WRITE_GRANTED, "https://www.googleapis.com/auth/drive.readonly"),
    ],
)
def test_write_authorization_with_other_scopes_is_rejected(make_manager, granted):
    store = MemoryTokenStore()
    manager = make_manager(
        read_credentials(), FakeCredentials(granted, "sub-a", "w"), store=store
    )
    manager.start()

    assert manager.status().state is State.DISCONNECTED
    assert store.saves == 0


def test_read_without_refresh_token_is_rejected(make_manager):
    store = MemoryTokenStore()
    manager = make_manager(
        read_credentials(refresh_token=None), write_credentials(), store=store
    )
    manager.start()

    assert manager.status().state is State.DISCONNECTED
    assert manager.status().error == "authorization_failed"
    assert len(manager.script.requests) == 1
    assert store.saves == 0


def test_write_without_refresh_token_is_rejected(make_manager):
    store = MemoryTokenStore()
    manager = make_manager(
        read_credentials(), write_credentials(refresh_token=None), store=store
    )
    manager.start()

    assert manager.status().state is State.DISCONNECTED
    assert manager.status().error == "authorization_failed"
    assert store.saves == 0


# Identidad -----------------------------------------------------------------

def test_different_accounts_in_read_and_write_are_rejected(make_manager):
    store = MemoryTokenStore()
    manager = make_manager(
        read_credentials("sub-a"), write_credentials("sub-b"), store=store
    )
    manager.start()

    status = manager.status()
    assert status.state is State.DISCONNECTED
    assert status.error == "account_mismatch"
    assert "misma cuenta" in status.message
    assert store.saves == 0


def test_same_email_is_not_enough_identity_comes_only_from_sub(make_manager, drive):
    # El correo es solo para mostrar: dos sub distintos se rechazan igual.
    drive.about_response = {"user": {"emailAddress": "misma@example.com"}}
    manager = make_manager(read_credentials("sub-a"), write_credentials("sub-b"))
    manager.start()

    assert manager.status().error == "account_mismatch"


@pytest.mark.parametrize("position", ["read", "write"])
def test_missing_id_token_stops_the_connection(make_manager, position):
    store = MemoryTokenStore()
    read = read_credentials(None if position == "read" else "sub-a")
    write = write_credentials(None if position == "write" else "sub-a")
    manager = make_manager(read, write, store=store)
    manager.start()

    assert manager.status().state is State.DISCONNECTED
    assert manager.status().error == "identity_unverified"
    assert store.saves == 0


def test_id_token_for_another_audience_stops_the_connection(make_manager):
    store = MemoryTokenStore()
    forged = FakeCredentials(READ_GRANTED, "sub-a", "r", audience="otro-cliente")
    manager = make_manager(forged, store=store)
    manager.start()

    assert manager.status().error == "identity_unverified"
    assert store.saves == 0


def test_failed_reconnection_keeps_nothing_half_done(make_manager):
    store = MemoryTokenStore()
    make_manager(read_credentials("sub-a"), write_credentials("sub-a"), store=store).start()
    before = json.dumps(store.data, sort_keys=True)

    manager = make_manager(read_credentials("sub-b"), AccessDeniedError(), store=store)
    manager.disconnect()
    assert store.data is None

    manager.start()
    assert store.data is None
    assert before  # la conexión anterior se borró solo por la desconexión explícita


def test_display_data_failure_does_not_block_the_connection(make_manager, drive):
    from drive_fakes import http_error

    drive.read.failures.append(http_error(403, ["forbidden"]))
    manager = make_manager(read_credentials(), write_credentials())
    manager.start()

    status = manager.status()
    assert status.state is State.CONNECTED
    assert status.account.sub == "sub-a"
    assert status.account.email is None


# Verificación real del ID token (firma local, sin red) ----------------------

def _signed_token(claims, key=None):
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from google.auth import crypt, jwt

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "prueba")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    signer = crypt.RSASigner.from_string(pem, key_id="clave-1")
    token = jwt.encode(signer, claims).decode("ascii")
    return token, certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


class _CertsRequest:
    def __init__(self, certs):
        self.certs = certs

    def __call__(self, url, method="GET", headers=None, **kwargs):
        class Response:
            status = 200
            data = json.dumps(self.certs).encode()
            headers = {}

        return Response()


def _claims(**changes):
    import time

    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": DESKTOP_CLIENT_ID,
        "sub": "sub-firmado",
        "iat": now,
        "exp": now + 600,
    }
    claims.update(changes)
    return claims


def test_signed_id_token_is_accepted():
    token, cert = _signed_token(_claims())
    claims = verify_google_id_token(
        token, DESKTOP_CLIENT_ID, request=_CertsRequest({"clave-1": cert})
    )
    assert claims["sub"] == "sub-firmado"


def test_id_token_signed_by_another_key_is_rejected():
    token, _ = _signed_token(_claims())
    _, other_cert = _signed_token(_claims())

    with pytest.raises(IdentityError):
        verify_google_id_token(
            token, DESKTOP_CLIENT_ID, request=_CertsRequest({"clave-1": other_cert})
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"aud": "otro-cliente"},
        {"iss": "https://atacante.example.com"},
        {"exp": 1000, "iat": 900},
    ],
)
def test_id_token_with_bad_claims_is_rejected(changes):
    token, cert = _signed_token(_claims(**changes))

    with pytest.raises(IdentityError):
        verify_google_id_token(
            token, DESKTOP_CLIENT_ID, request=_CertsRequest({"clave-1": cert})
        )


def test_malformed_id_token_is_rejected():
    with pytest.raises(IdentityError):
        verify_google_id_token(
            "no-es-un-jwt", DESKTOP_CLIENT_ID, request=_CertsRequest({})
        )


# Desconexión ---------------------------------------------------------------

def test_disconnect_deletes_local_tokens(make_manager):
    store = MemoryTokenStore()
    manager = make_manager(read_credentials(), write_credentials(), store=store)
    manager.start()

    manager.disconnect()

    assert store.data is None
    assert manager.status().state is State.DISCONNECTED
    with pytest.raises(NotConnectedError):
        manager.services()


def test_disconnect_when_disconnected_is_harmless(make_manager):
    manager = make_manager()
    manager.disconnect()

    assert manager.status().state is State.DISCONNECTED


def test_disconnect_then_connect_another_account(make_manager, drive):
    store = MemoryTokenStore()
    manager = make_manager(
        read_credentials("sub-a"),
        write_credentials("sub-a"),
        read_credentials("sub-b"),
        write_credentials("sub-b"),
        store=store,
    )
    manager.start()
    assert manager.status().account.sub == "sub-a"

    manager.disconnect()
    manager.start()

    assert manager.status().account.sub == "sub-b"
    assert store.data["sub"] == "sub-b"


def test_any_account_is_accepted_there_is_no_allow_list(make_manager):
    for sub in ("cuenta-personal", "cuenta-workspace", "cuenta-nunca-vista"):
        manager = make_manager(read_credentials(sub), write_credentials(sub))
        manager.start()
        assert manager.status().state is State.CONNECTED
        assert manager.status().account.sub == sub


def test_disconnect_during_authorization_cancels_it(make_manager):
    store = MemoryTokenStore()
    holder = {}
    flow = FakeFlow(read_credentials(), on_run=lambda: holder["manager"].disconnect())
    manager = make_manager(flow, write_credentials(), store=store)
    holder["manager"] = manager

    manager.start()

    assert store.saves == 0
    assert manager.status().state is State.DISCONNECTED


def test_expired_authorization_requires_reconnecting(connected):
    connected.mark_expired()

    status = connected.status()
    assert status.state is State.RECONNECT_REQUIRED
    assert status.account.sub == "sub-a"


def test_can_reconnect_after_expiry(make_manager):
    store = MemoryTokenStore()
    manager = make_manager(
        read_credentials("sub-a"), write_credentials("sub-a"),
        read_credentials("sub-b"), write_credentials("sub-b"),
        store=store,
    )
    manager.start()
    manager.mark_expired()

    manager.start()

    assert manager.status().state is State.CONNECTED
    assert manager.status().account.sub == "sub-b"


def test_failed_reauthorization_keeps_the_requirement_to_reconnect(make_manager):
    store = MemoryTokenStore()
    manager = make_manager(
        read_credentials("sub-a"), write_credentials("sub-a"),
        AccessDeniedError(),
        store=store,
    )
    manager.start()
    manager.mark_expired()
    manager.start()

    # La conexión guardada no se pierde por un intento rechazado.
    assert manager.status().state is State.RECONNECT_REQUIRED
    assert store.data["sub"] == "sub-a"


def test_unexpected_error_during_authorization_is_controlled(make_manager, caplog):
    store = MemoryTokenStore()
    secret = "refresco-en-el-mensaje-de-error"
    manager = make_manager(RuntimeError(secret), store=store)

    with caplog.at_level(logging.DEBUG):
        manager.start()

    assert manager.status().state is State.DISCONNECTED
    assert secret not in manager.status().message
    assert secret not in caplog.text


# Protección de tokens -------------------------------------------------------

def test_status_never_contains_tokens_or_secrets(connected):
    dumped = repr(connected.status())

    for secret in (
        "refresh-lectura", "refresh-escritura",
        "token-de-acceso-de-prueba", DESKTOP_SECRET, "idtoken:",
    ):
        assert secret not in dumped


def test_tokens_are_never_logged(make_manager, caplog):
    with caplog.at_level(logging.DEBUG):
        manager = make_manager(read_credentials(), write_credentials())
        manager.start()
        manager.services()
        manager.disconnect()

    for secret in ("refresh-lectura", "refresh-escritura", "idtoken:", DESKTOP_SECRET):
        assert secret not in caplog.text


def test_stored_bundle_has_no_access_tokens_or_client_secret(make_manager):
    store = MemoryTokenStore()
    make_manager(read_credentials(), write_credentials(), store=store).start()

    dumped = json.dumps(store.data)

    assert "token-de-acceso-de-prueba" not in dumped
    assert DESKTOP_SECRET not in dumped
    assert "idtoken:" not in dumped
    assert store.data["read_refresh_token"] == "refresh-lectura"
    assert store.data["write_refresh_token"] == "refresh-escritura"


def test_store_failure_when_saving_is_controlled(make_manager):
    class Broken(MemoryTokenStore):
        def save(self, data):
            raise TokenStoreError("sin disco")

    manager = make_manager(read_credentials(), write_credentials(), store=Broken())
    manager.start()

    assert manager.status().state is State.DISCONNECTED
    assert manager.status().message


def test_engine_auth_error_is_reported(make_manager):
    manager = make_manager(DriveAuthError("no se completó"))
    manager.start()

    assert manager.status().state is State.DISCONNECTED
    assert manager.status().message
    assert write_credentials  # el fallo no llega a pedir escritura
    assert len(manager.script.requests) == 1
