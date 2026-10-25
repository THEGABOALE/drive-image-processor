
import json
import logging
import os
import ssl
import webbrowser
from datetime import datetime, timedelta, timezone

import pytest
import requests
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import WSGITimeoutError
from oauthlib.oauth2.rfc6749.errors import AccessDeniedError

from image_processor.google_drive import auth
from image_processor.google_drive.errors import DriveAuthError, DriveNetworkError


ACCESS = "ya29.SECRETO-ACCESO-PRUEBA"
NEW_ACCESS = "ya29.SECRETO-ACCESO-RENOVADO"
REFRESH = "1//SECRETO-REFRESCO-PRUEBA"
CLIENT_SECRET = "SECRETO-CLIENTE-PRUEBA"
SECRETS = (ACCESS, NEW_ACCESS, REFRESH, CLIENT_SECRET)

OTHER_SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"


def expiry(hours):
    moment = datetime.now(timezone.utc) + timedelta(hours=hours)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_token(paths, hours=1, scopes=(auth.READONLY_SCOPE,), refresh=REFRESH):
    paths.directory.mkdir(parents=True, exist_ok=True)
    info = {
        "token": ACCESS,
        "refresh_token": refresh,
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cliente-prueba.apps.googleusercontent.com",
        "client_secret": CLIENT_SECRET,
        "scopes": list(scopes),
        "expiry": expiry(hours),
    }
    paths.token.write_text(json.dumps(info), encoding="utf-8")


def write_client_secret(paths):
    paths.directory.mkdir(parents=True, exist_ok=True)
    paths.client_secret.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "cliente-prueba.apps.googleusercontent.com",
                    "client_secret": CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )


def flow_credentials(scopes=(auth.READONLY_SCOPE,)):
    return Credentials(
        token=ACCESS,
        refresh_token=REFRESH,
        token_uri="https://oauth2.googleapis.com/token",
        client_id="cliente-prueba.apps.googleusercontent.com",
        client_secret=CLIENT_SECRET,
        scopes=list(scopes),
        expiry=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
    )


class FakeFlow:
    def __init__(self, credentials=None, error=None):
        self.credentials = credentials
        self.error = error
        self.kwargs = None

    def run_local_server(self, **kwargs):
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.credentials


def flow_factory(flow, calls):
    def factory(path, scopes):
        calls.append((path, tuple(scopes)))
        return flow

    return factory


def forbidden_request():
    raise AssertionError("no debía contactar con Google")


@pytest.fixture
def paths(tmp_path):
    return auth.secret_paths(tmp_path / ".secrets")


@pytest.fixture
def no_network_refresh(monkeypatch):
    calls = []

    def refresh(self, request):
        calls.append(request)
        self.token = NEW_ACCESS
        self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(Credentials, "refresh", refresh)
    return calls


# Ubicación de los secretos -------------------------------------------

def test_secret_paths_default_and_environment(tmp_path, monkeypatch):
    monkeypatch.delenv(auth.SECRETS_DIR_ENV, raising=False)
    default = auth.secret_paths()

    assert default.directory == auth.DEFAULT_SECRETS_DIR
    assert default.token.name == "token.json"
    assert default.client_secret.name == "credentials.json"

    monkeypatch.setenv(auth.SECRETS_DIR_ENV, str(tmp_path / "privado"))
    assert auth.secret_paths().directory == tmp_path / "privado"

    assert auth.secret_paths(tmp_path / "otro").directory == tmp_path / "otro"


def test_only_read_only_scope_is_requested():
    assert auth.SCOPES == ("https://www.googleapis.com/auth/drive.readonly",)


# 1. Token válido -----------------------------------------------------

def test_valid_token_is_reused(paths):
    write_token(paths, hours=1)
    before = paths.token.read_bytes()

    credentials = auth.get_credentials(paths, request_factory=forbidden_request)

    assert credentials.valid
    assert credentials.token == ACCESS
    assert paths.token.read_bytes() == before


def test_auth_command_reuses_valid_token_without_browser(paths):
    write_token(paths, hours=1)
    calls = []

    auth.get_credentials(
        paths,
        interactive=True,
        flow_factory=flow_factory(FakeFlow(), calls),
        request_factory=forbidden_request,
    )

    assert calls == []


# 2. Renovación -------------------------------------------------------

def test_expired_token_is_refreshed_and_saved(paths, no_network_refresh):
    write_token(paths, hours=-1)

    credentials = auth.get_credentials(paths, request_factory=lambda: "solicitud")

    assert no_network_refresh == ["solicitud"]
    assert credentials.token == NEW_ACCESS

    saved = json.loads(paths.token.read_text(encoding="utf-8"))
    assert saved["token"] == NEW_ACCESS
    assert saved["refresh_token"] == REFRESH
    assert list(paths.directory.glob("*.tmp")) == []


def test_network_error_during_refresh(paths, monkeypatch):
    write_token(paths, hours=-1)

    def refresh(self, request):
        raise TransportError("sin conexión")

    monkeypatch.setattr(Credentials, "refresh", refresh)

    with pytest.raises(DriveNetworkError):
        auth.get_credentials(paths, request_factory=lambda: None)


# 3. Ausencia de credenciales -----------------------------------------

def test_missing_token_requires_auth_command(paths):
    with pytest.raises(DriveAuthError, match="drive_cli auth"):
        auth.get_credentials(paths)


def test_missing_client_secret_is_explained(paths):
    with pytest.raises(DriveAuthError, match="credentials.json"):
        auth.get_credentials(paths, interactive=True)


def test_interactive_login_saves_token(paths):
    write_client_secret(paths)
    flow = FakeFlow(flow_credentials())
    calls = []

    credentials = auth.get_credentials(
        paths, interactive=True, flow_factory=flow_factory(flow, calls)
    )

    assert calls == [(str(paths.client_secret), auth.SCOPES)]
    assert flow.kwargs["port"] == 0
    assert flow.kwargs["open_browser"] is True
    assert credentials.token == ACCESS
    assert json.loads(paths.token.read_text("utf-8"))["refresh_token"] == REFRESH


@pytest.mark.skipif(os.name == "nt", reason="Permisos POSIX")
def test_saved_token_is_private(paths):
    auth.save_token(flow_credentials(), paths.token)

    assert paths.token.stat().st_mode & 0o777 == 0o600
    assert paths.directory.stat().st_mode & 0o777 == 0o700


def test_failed_token_save_keeps_previous_token(paths, monkeypatch):
    write_token(paths, hours=1)
    before = paths.token.read_bytes()

    def failing_replace(source, target):
        raise OSError("disco lleno")

    monkeypatch.setattr(auth.os, "replace", failing_replace)

    with pytest.raises(OSError):
        auth.save_token(flow_credentials(), paths.token)

    assert paths.token.read_bytes() == before
    assert list(paths.directory.glob("*.tmp")) == []


def test_logout_removes_local_token(paths):
    write_token(paths)

    assert auth.logout(paths) is True
    assert not paths.token.exists()
    assert auth.logout(paths) is False


# 4. Errores de autorización ------------------------------------------

def test_revoked_token_is_not_retried(paths, monkeypatch):
    write_token(paths, hours=-1)
    attempts = []

    def refresh(self, request):
        attempts.append(1)
        raise RefreshError(
            f"invalid_grant: Token has been expired or revoked. {REFRESH}"
        )

    monkeypatch.setattr(Credentials, "refresh", refresh)

    with pytest.raises(DriveAuthError, match="revocada") as error:
        auth.get_credentials(paths, request_factory=lambda: None)

    assert attempts == [1]
    assert REFRESH not in str(error.value)
    assert error.value.__cause__ is None


def test_revoked_token_falls_back_to_login_in_auth_command(paths, monkeypatch):
    write_token(paths, hours=-1)
    write_client_secret(paths)

    def refresh(self, request):
        raise RefreshError("invalid_grant")

    monkeypatch.setattr(Credentials, "refresh", refresh)
    calls = []

    credentials = auth.get_credentials(
        paths,
        interactive=True,
        flow_factory=flow_factory(FakeFlow(flow_credentials()), calls),
        request_factory=lambda: None,
    )

    assert len(calls) == 1
    assert credentials.valid


@pytest.mark.parametrize(
    "error",
    [
        AccessDeniedError(description="access_denied"),
        AttributeError("sin código"),
    ],
)
def test_cancelled_or_denied_login(paths, error):
    write_client_secret(paths)

    with pytest.raises(DriveAuthError, match="cancelada"):
        auth.get_credentials(
            paths,
            interactive=True,
            flow_factory=flow_factory(FakeFlow(error=error), []),
        )

    assert not paths.token.exists()


def test_login_without_read_only_scope_is_rejected(paths):
    write_client_secret(paths)

    with pytest.raises(DriveAuthError, match="solo lectura"):
        auth.get_credentials(
            paths,
            interactive=True,
            flow_factory=flow_factory(
                FakeFlow(flow_credentials(scopes=(OTHER_SCOPE,))), []
            ),
        )

    assert not paths.token.exists()


def test_token_without_read_only_scope_requires_auth(paths):
    write_token(paths, scopes=(OTHER_SCOPE,))

    with pytest.raises(DriveAuthError, match="solo lectura"):
        auth.get_credentials(paths, request_factory=forbidden_request)


@pytest.mark.parametrize(
    "content",
    ["no es JSON", "{}", json.dumps({"token": ACCESS})],
)
def test_damaged_token_is_rejected_without_its_content(paths, content):
    paths.directory.mkdir(parents=True)
    paths.token.write_text(content, encoding="utf-8")

    with pytest.raises(DriveAuthError, match="no es válido") as error:
        auth.get_credentials(paths)

    assert ACCESS not in str(error.value)


def test_invalid_client_secret_file(paths):
    paths.directory.mkdir(parents=True)
    paths.client_secret.write_text("{}", encoding="utf-8")

    def factory(path, scopes):
        raise ValueError(f"Client secrets must be for a web or installed app. {CLIENT_SECRET}")

    with pytest.raises(DriveAuthError, match="no es válido") as error:
        auth.get_credentials(paths, interactive=True, flow_factory=factory)

    assert CLIENT_SECRET not in str(error.value)


# 15. Ausencia de credenciales en mensajes y registros ----------------

def test_secrets_never_reach_output_or_logs(
    paths, monkeypatch, capsys, caplog, no_network_refresh
):
    caplog.set_level(logging.DEBUG)
    write_client_secret(paths)

    # Inicio de sesión, renovación y errores.
    auth.get_credentials(
        paths,
        interactive=True,
        flow_factory=flow_factory(FakeFlow(flow_credentials()), []),
    )

    write_token(paths, hours=-1)
    auth.get_credentials(paths, request_factory=lambda: None)

    def revoked(self, request):
        raise RefreshError(f"invalid_grant {REFRESH} {ACCESS}")

    monkeypatch.setattr(Credentials, "refresh", revoked)
    write_token(paths, hours=-1)

    with pytest.raises(DriveAuthError) as error:
        auth.get_credentials(paths, request_factory=lambda: None)

    captured = capsys.readouterr()
    texts = [captured.out, captured.err, caplog.text, str(error.value)]

    for secret in SECRETS:
        assert all(secret not in text for text in texts)


# --- Revisión GD-01 ----------------------------------------------------

DAMAGED = ('{"token": "' + ACCESS + '", "refresh_token": "' + REFRESH + '" (truncado').encode("utf-8")


def write_damaged_token(paths):
    paths.directory.mkdir(parents=True, exist_ok=True)
    paths.token.write_bytes(DAMAGED)


def backups(paths):
    return sorted(paths.directory.glob("token.damaged-*.json"))


def test_client_file_is_credentials_json(tmp_path):
    assert auth.secret_paths(tmp_path).client_secret.name == "credentials.json"


# GD01-R1: token dañado --------------------------------------------------

def test_damaged_token_is_rejected_without_interaction(paths):
    write_damaged_token(paths)

    with pytest.raises(DriveAuthError, match="no es válido") as error:
        auth.get_credentials(paths, interactive=False)

    assert paths.token.read_bytes() == DAMAGED
    assert backups(paths) == []
    assert ACCESS not in str(error.value)


def test_damaged_token_starts_new_authorization(paths):
    write_damaged_token(paths)
    write_client_secret(paths)
    calls = []
    notices = []

    credentials = auth.get_credentials(
        paths,
        interactive=True,
        flow_factory=flow_factory(FakeFlow(flow_credentials()), calls),
        notices=notices,
    )

    # Nunca se trató como válido: se pidió una autorización nueva.
    assert len(calls) == 1
    assert credentials.valid

    saved = json.loads(paths.token.read_text(encoding="utf-8"))
    assert saved["token"] == ACCESS

    # El archivo anterior se conserva, sin cambios, en la carpeta privada.
    [backup] = backups(paths)
    assert backup.read_bytes() == DAMAGED
    assert len(notices) == 1
    assert backup.name in notices[0]
    assert all(secret not in notices[0] for secret in SECRETS)
    assert list(paths.directory.glob("*.tmp")) == []


def test_cancelled_authorization_keeps_damaged_token(paths):
    write_damaged_token(paths)
    write_client_secret(paths)

    with pytest.raises(DriveAuthError) as error:
        auth.get_credentials(
            paths,
            interactive=True,
            flow_factory=flow_factory(
                FakeFlow(error=AccessDeniedError(description=ACCESS)), []
            ),
        )

    assert paths.token.read_bytes() == DAMAGED
    assert backups(paths) == []
    assert all(secret not in str(error.value) for secret in SECRETS)
    assert error.value.__cause__ is None


def test_damaged_token_is_not_overwritten_if_it_cannot_be_preserved(
    paths, monkeypatch
):
    write_damaged_token(paths)
    write_client_secret(paths)

    def failing_rename(source, target):
        raise PermissionError("en uso")

    monkeypatch.setattr(auth.os, "rename", failing_rename)

    with pytest.raises(DriveAuthError, match="no se pudo conservar"):
        auth.get_credentials(
            paths,
            interactive=True,
            flow_factory=flow_factory(FakeFlow(flow_credentials()), []),
        )

    assert paths.token.read_bytes() == DAMAGED


# GD01-R2: errores de red durante OAuth ----------------------------------

@pytest.mark.parametrize(
    "error, expected",
    [
        (requests.exceptions.ConnectionError("sin conexión"), "red"),
        (requests.exceptions.ConnectTimeout("tiempo"), "tiempo"),
        (requests.exceptions.ReadTimeout("tiempo"), "tiempo"),
        (requests.exceptions.ProxyError("proxy"), "red"),
        (requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED"), "certificado"),
        (ssl.SSLCertVerificationError("certificado"), "certificado"),
        (OSError("red inaccesible"), "red"),
    ],
)
def test_network_errors_during_login_are_controlled(paths, error, expected):
    write_client_secret(paths)
    flow = FakeFlow(error=error)
    calls = []

    with pytest.raises(DriveNetworkError, match=expected) as raised:
        auth.get_credentials(
            paths, interactive=True, flow_factory=flow_factory(flow, calls)
        )

    # Sin traceback encadenado y sin reintentar el flujo.
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    assert len(calls) == 1
    assert not paths.token.exists()


def test_ssl_message_does_not_suggest_disabling_verification(paths):
    write_client_secret(paths)

    with pytest.raises(DriveNetworkError) as raised:
        auth.get_credentials(
            paths,
            interactive=True,
            flow_factory=flow_factory(
                FakeFlow(error=requests.exceptions.SSLError("x")), []
            ),
        )

    assert "no se desactiva" in str(raised.value)


@pytest.mark.parametrize(
    "error, expected",
    [
        (WSGITimeoutError("Timed out"), "tiempo"),
        (webbrowser.Error("could not locate runnable browser"), "navegador"),
        (KeyError("token_uri"), "no es válido"),
    ],
)
def test_other_login_failures_are_controlled(paths, error, expected):
    write_client_secret(paths)

    with pytest.raises(DriveAuthError, match=expected) as raised:
        auth.get_credentials(
            paths,
            interactive=True,
            flow_factory=flow_factory(FakeFlow(error=error), []),
        )

    assert raised.value.__cause__ is None


def test_declined_scope_warning_does_not_expose_tokens(paths):
    write_client_secret(paths)

    # oauthlib lanza Warning si se concede otro scope; incluye el token.
    warning = Warning('Scope has changed from "drive.readonly" to "".')
    warning.token = {"access_token": ACCESS, "refresh_token": REFRESH}

    with pytest.raises(DriveAuthError, match="no concedió") as raised:
        auth.get_credentials(
            paths,
            interactive=True,
            flow_factory=flow_factory(FakeFlow(error=warning), []),
        )

    assert raised.value.__cause__ is None
    assert all(secret not in str(raised.value) for secret in SECRETS)
    assert not paths.token.exists()


def test_http_library_error_in_real_token_exchange(paths, monkeypatch):
    """
    Intercambio real del código por tokens (InstalledAppFlow +
    requests-oauthlib) con la biblioteca HTTP fallando por SSL.
    """
    write_client_secret(paths)
    sent = []

    def failing_send(self, request, **kwargs):
        sent.append(request.url)
        raise requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")

    monkeypatch.setattr(requests.Session, "send", failing_send)

    def real_flow(path, scopes):
        flow = auth.InstalledAppFlow.from_client_secrets_file(path, scopes)
        flow.redirect_uri = "http://localhost:8080/"

        def run_local_server(**kwargs):
            # Lo que hace run_local_server tras recibir el código.
            return flow.fetch_token(code="codigo-de-prueba")

        flow.run_local_server = run_local_server
        return flow

    with pytest.raises(DriveNetworkError, match="certificado"):
        auth.get_credentials(paths, interactive=True, flow_factory=real_flow)

    assert sent == ["https://oauth2.googleapis.com/token"]


def test_http_library_error_during_real_refresh(paths, monkeypatch):
    """Renovación real (google.auth Request) con la biblioteca HTTP fallando."""

    write_token(paths, hours=-1)

    def failing_request(self, method, url, **kwargs):
        raise requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")

    monkeypatch.setattr(requests.Session, "request", failing_request)

    with pytest.raises(DriveNetworkError, match="certificado") as raised:
        auth.get_credentials(paths)

    assert raised.value.__cause__ is None
