"""
Utilidades de las pruebas del backend web. Ninguna prueba abre un
navegador ni contacta a Google: el flujo OAuth, la verificación del ID
token y Drive se simulan (Drive con FakeDriveServer, el doble de las
pruebas del motor).
"""

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

for extra in (REPO, REPO / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

try:
    import fastapi  # noqa: F401
    import httpx  # noqa: F401
except ImportError:
    # Sin web/backend/requirements.txt solo corren las pruebas del motor.
    collect_ignore_glob = ["test_*.py"]


PROJECT_ID = "drive-image-processor-dev"
PROJECT_NUMBER = "123456789012"
DESKTOP_CLIENT_ID = f"{PROJECT_NUMBER}-desktop.apps.googleusercontent.com"
PICKER_CLIENT_ID = f"{PROJECT_NUMBER}-web.apps.googleusercontent.com"
PICKER_API_KEY = "clave-publica-de-prueba"
DESKTOP_SECRET = "secreto-de-escritorio-de-prueba"


def desktop_config(
    project_id=PROJECT_ID, client_id=DESKTOP_CLIENT_ID, kind="installed"
):
    return {
        kind: {
            "client_id": client_id,
            "client_secret": DESKTOP_SECRET,
            "project_id": project_id,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


@pytest.fixture
def make_environ(tmp_path):
    """Variables de entorno válidas para un entorno, con cambios opcionales."""

    def build(environment="development", client=None, **changes):
        suffix = "dev" if environment == "development" else "prod"
        project_id = f"drive-image-processor-{suffix}"
        path = tmp_path / f"desktop_{suffix}.json"
        path.write_text(
            json.dumps(client or desktop_config(project_id=project_id)),
            encoding="utf-8",
        )
        environ = {
            "DIP_ENV": environment,
            "LOCALAPPDATA": str(tmp_path / "LocalAppData"),
            "GOOGLE_CLOUD_PROJECT_ID": project_id,
            "GOOGLE_CLOUD_PROJECT_NUMBER": PROJECT_NUMBER,
            "GOOGLE_DESKTOP_CLIENT_FILE": str(path),
            "GOOGLE_PICKER_CLIENT_ID": PICKER_CLIENT_ID,
            "GOOGLE_PICKER_API_KEY": PICKER_API_KEY,
        }
        environ.update(changes)
        return {k: v for k, v in environ.items() if v is not None}

    return build


@pytest.fixture
def settings(make_environ, tmp_path):
    from web.backend.app.config import load_settings

    return load_settings(make_environ(), env_file=tmp_path / "no-existe.env")


class MemoryTokenStore:
    """Otra implementación de TokenStore: demuestra que es sustituible."""

    def __init__(self, data=None, load_error=None):
        self.data = data
        self.load_error = load_error
        self.saves = 0

    def load(self):
        if self.load_error is not None:
            raise self.load_error
        return self.data

    def save(self, data):
        self.saves += 1
        self.data = data

    def clear(self):
        existed = self.data is not None
        self.data = None
        return existed


class FakeCredentials:
    def __init__(self, granted, sub, refresh_token, audience=DESKTOP_CLIENT_ID):
        self.granted_scopes = list(granted)
        self.scopes = list(granted)
        self.id_token = f"idtoken:{sub}:{audience}" if sub else None
        self.refresh_token = refresh_token
        self.token = "token-de-acceso-de-prueba"
        self.valid = True


class FakeFlow:
    def __init__(self, result, on_run=None):
        self.result = result
        self.on_run = on_run
        self.kwargs = None

    def run_local_server(self, **kwargs):
        self.kwargs = kwargs
        if self.on_run is not None:
            self.on_run()
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


READ_GRANTED = ("https://www.googleapis.com/auth/drive.readonly", "openid")
WRITE_GRANTED = ("https://www.googleapis.com/auth/drive.file", "openid")


def read_credentials(sub="sub-a", refresh_token="refresh-lectura"):
    return FakeCredentials(READ_GRANTED, sub, refresh_token)


def write_credentials(sub="sub-a", refresh_token="refresh-escritura"):
    return FakeCredentials(WRITE_GRANTED, sub, refresh_token)


class FlowScript:
    """flow_factory: entrega los resultados en orden y registra cada pedido."""

    def __init__(self, *results):
        self.pending = list(results)
        self.requests = []

    def __call__(self, client_file, scopes, login_hint=None):
        self.requests.append(
            {"file": client_file, "scopes": scopes, "login_hint": login_hint}
        )
        item = self.pending.pop(0)
        return item if isinstance(item, FakeFlow) else FakeFlow(item)


def fake_verify_id_token(token, audience):
    from web.backend.app.connection import IdentityError

    _, sub, token_audience = token.split(":")
    if token_audience != audience:
        raise IdentityError("audiencia inesperada")
    return {"sub": sub}


@pytest.fixture
def drive():
    from drive_fakes import FakeDriveServer, folder

    return FakeDriveServer(
        folders=[
            folder("origen", "Origen"),
            folder("destino", "Destino"),
            folder("ajena", "Ajena"),
        ],
        items={"foto": {"id": "foto", "name": "a.jpg", "mimeType": "image/jpeg"}},
        app_folders={"origen", "destino"},
    )


@pytest.fixture
def make_manager(settings, drive):
    from web.backend.app.connection import ConnectionManager

    def service_factory(credentials):
        return drive.read if credentials.refresh_token == "refresh-lectura" else drive.write

    def build(*results, store=None, settings_=None, **kwargs):
        script = FlowScript(*results)
        manager = ConnectionManager(
            settings_ or settings,
            store if store is not None else MemoryTokenStore(),
            flow_factory=script,
            verify_id_token=fake_verify_id_token,
            service_factory=service_factory,
            start_thread=kwargs.pop("start_thread", lambda target: target()),
            **kwargs,
        )
        manager.script = script
        return manager

    return build


@pytest.fixture
def connected(make_manager):
    manager = make_manager(read_credentials(), write_credentials())
    manager.start()
    return manager


@pytest.fixture
def make_client():
    from fastapi.testclient import TestClient

    from web.backend.app.main import create_app

    def build(settings, manager, origin="http://localhost:5173"):
        app = create_app(settings, manager)
        client = TestClient(
            app, base_url="http://localhost:8000", raise_server_exceptions=False
        )
        client.headers["Origin"] = origin
        client.csrf_token = client.get("/api/status").json()["csrf_token"]
        client.headers["X-CSRF-Token"] = client.csrf_token
        return client

    return build
