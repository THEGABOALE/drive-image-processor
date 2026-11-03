"""
Configuración por entorno de la aplicación web.

El entorno (DIP_ENV: development o production) elige el archivo
web/backend/.env.dev.local o .env.production.local, excluidos de Git, y la
carpeta de datos
%LOCALAPPDATA%/DriveImageProcessor/<entorno>. Las variables reales tienen
prioridad sobre el archivo. Ninguna credencial está en el código.

Los dos clientes OAuth deben ser del proyecto de Google Cloud configurado:
Google solo comparte las autorizaciones drive.file entre clientes del mismo
proyecto. Los mensajes de error nombran variables, nunca valores secretos.

La aplicación es local: siempre escucha en loopback y la dirección no se
puede cambiar por configuración. Hosts y orígenes están en Settings para
poder desplegarla en un servidor sin tocar el motor.
"""

import ipaddress
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


class ConfigError(Exception):
    """Configuración ausente o inconsistente."""


BACKEND_DIR = Path(__file__).resolve().parents[1]

BIND_HOST = "127.0.0.1"
PORT = 8000
DEV_FRONTEND_ORIGIN = "http://localhost:5173"
LOCAL_ORIGIN = f"http://localhost:{PORT}"

ENVIRONMENTS = ("development", "production")

_ENV_FILES = {
    "development": ".env.dev.local",
    "production": ".env.production.local",
}

# Sufijo del proyecto del otro entorno, que nunca debe usarse en este.
_FOREIGN_PROJECT_SUFFIX = {"development": "-prod", "production": "-dev"}

_REQUIRED = (
    "LOCALAPPDATA",
    "GOOGLE_CLOUD_PROJECT_ID",
    "GOOGLE_CLOUD_PROJECT_NUMBER",
    "GOOGLE_DESKTOP_CLIENT_FILE",
    "GOOGLE_PICKER_CLIENT_ID",
    "GOOGLE_PICKER_API_KEY",
)


@dataclass(frozen=True)
class Settings:
    environment: str
    project_id: str
    project_number: str
    desktop_client_file: Path
    desktop_client_id: str
    # Cliente de escritorio: token_uri, client_id y client_secret.
    desktop_client: dict = field(repr=False)
    picker_client_id: str
    picker_api_key: str = field(repr=False)
    data_dir: Path
    bind_host: str
    port: int
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]
    app_url: str
    frontend_dist: Path


def ensure_loopback(host: str) -> str:
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = False

    if not is_loopback:
        raise ConfigError(
            "El servidor local solo puede escuchar en una dirección loopback."
        )

    return host


def _read_desktop_client(path: Path, project_id: str, project_number: str) -> dict:
    invalid = ConfigError(
        f"GOOGLE_DESKTOP_CLIENT_FILE ({path}) debe ser el JSON de un cliente "
        "OAuth de tipo aplicación de escritorio."
    )

    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Sin detalles: el archivo contiene un secreto.
        raise invalid from None

    client = content.get("installed") if isinstance(content, dict) else None

    if not isinstance(client, dict) or not all(
        client.get(name) for name in ("client_id", "client_secret", "token_uri")
    ):
        raise invalid

    if client.get("project_id") != project_id:
        raise ConfigError(
            f"El cliente de GOOGLE_DESKTOP_CLIENT_FILE no pertenece al proyecto "
            f"{project_id} (GOOGLE_CLOUD_PROJECT_ID)."
        )

    if not client["client_id"].startswith(f"{project_number}-"):
        raise ConfigError(
            "El cliente de escritorio no pertenece al proyecto "
            "GOOGLE_CLOUD_PROJECT_NUMBER."
        )

    return client


def load_settings(environ=None, env_file: Path | None = None) -> Settings:
    environ = os.environ if environ is None else environ
    environment = environ.get("DIP_ENV", "development")

    if environment not in ENVIRONMENTS:
        raise ConfigError("DIP_ENV debe ser development o production.")

    path = env_file if env_file is not None else (
        BACKEND_DIR / _ENV_FILES[environment]
    )
    values = {**dotenv_values(path), **environ}

    missing = [name for name in _REQUIRED if not values.get(name)]

    if missing:
        raise ConfigError(
            f"Faltan variables de configuración ({environment}): "
            f"{', '.join(missing)}. Ver web/backend/.env.example."
        )

    project_id = values["GOOGLE_CLOUD_PROJECT_ID"]
    project_number = values["GOOGLE_CLOUD_PROJECT_NUMBER"]
    picker_client_id = values["GOOGLE_PICKER_CLIENT_ID"]

    if not project_number.isdigit():
        raise ConfigError("GOOGLE_CLOUD_PROJECT_NUMBER debe ser numérico.")

    if project_id.endswith(_FOREIGN_PROJECT_SUFFIX[environment]):
        raise ConfigError(
            f"El proyecto {project_id} es de otro entorno: no se usa en "
            f"{environment}."
        )

    if not picker_client_id.startswith(f"{project_number}-"):
        raise ConfigError(
            "GOOGLE_PICKER_CLIENT_ID no pertenece al proyecto "
            "GOOGLE_CLOUD_PROJECT_NUMBER."
        )

    client_file = Path(values["GOOGLE_DESKTOP_CLIENT_FILE"])
    client = _read_desktop_client(client_file, project_id, project_number)

    if client["client_id"] == picker_client_id:
        raise ConfigError(
            "Los clientes OAuth de escritorio y web deben ser distintos."
        )

    development = environment == "development"
    origins = {LOCAL_ORIGIN}
    hosts = {f"localhost:{PORT}"}

    if development:
        origins.add(DEV_FRONTEND_ORIGIN)
        hosts.add("localhost:5173")

    return Settings(
        environment=environment,
        project_id=project_id,
        project_number=project_number,
        desktop_client_file=client_file,
        desktop_client_id=client["client_id"],
        desktop_client=client,
        picker_client_id=picker_client_id,
        picker_api_key=values["GOOGLE_PICKER_API_KEY"],
        data_dir=Path(values["LOCALAPPDATA"]) / "DriveImageProcessor" / environment,
        bind_host=BIND_HOST,
        port=PORT,
        allowed_hosts=frozenset(hosts),
        allowed_origins=frozenset(origins),
        app_url=DEV_FRONTEND_ORIGIN if development else LOCAL_ORIGIN,
        frontend_dist=BACKEND_DIR.parent / "frontend" / "dist",
    )
