
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import pytest
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials
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
    assert default.client_secret.name == "client_secret.json"

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
    with pytest.raises(DriveAuthError, match="client_secret.json"):
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
