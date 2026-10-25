"""
OAuth 2.0 para una aplicación local de escritorio.

Scope: drive.readonly. Es un scope restringido de Google: sirve
para esta prueba local con una cuenta autorizada como usuario de
prueba, pero una aplicación pública que lo use puede requerir la
verificación de Google. Los permisos mínimos de producción
(drive.file y Google Picker) se estudiarán más adelante.

Los secretos se guardan en una carpeta privada configurable
(por defecto .secrets/, excluida de Git):

- credentials.json: cliente OAuth de tipo "aplicación de
  escritorio", descargado de Google Cloud Console.
- token.json: token de acceso y de actualización de la cuenta.

Ningún mensaje ni registro incluye el contenido de esos archivos.
"""

import json
import os
import secrets
import ssl
import tempfile
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
from oauthlib.oauth2.rfc6749.errors import OAuth2Error

from .errors import DriveAuthError, DriveNetworkError


READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
SCOPES = (READONLY_SCOPE,)

SECRETS_DIR_ENV = "DRIVE_IMAGE_PROCESSOR_SECRETS_DIR"
DEFAULT_SECRETS_DIR = Path(".secrets")

CLIENT_SECRET_NAME = "credentials.json"
TOKEN_NAME = "token.json"

# Tiempo máximo para completar la autorización en el navegador.
LOGIN_TIMEOUT_SECONDS = 300

AUTH_COMMAND = "python -m image_processor.drive_cli auth"


@dataclass(frozen=True)
class SecretPaths:
    directory: Path
    client_secret: Path
    token: Path


def secret_paths(directory: Path | None = None) -> SecretPaths:
    """
    Ubicación de los secretos: el argumento, la variable de entorno
    DRIVE_IMAGE_PROCESSOR_SECRETS_DIR o .secrets/.
    """
    if directory is None:
        directory = os.environ.get(SECRETS_DIR_ENV) or DEFAULT_SECRETS_DIR

    directory = Path(directory)

    return SecretPaths(
        directory=directory,
        client_secret=directory / CLIENT_SECRET_NAME,
        token=directory / TOKEN_NAME,
    )


def _granted_scopes(credentials) -> set[str]:
    granted = getattr(credentials, "granted_scopes", None)
    return set(granted or credentials.scopes or [])


def _is_certificate_error(error: BaseException) -> bool:
    """¿El error (o su causa) es de verificación de certificado SSL?"""

    seen = set()

    while error is not None and id(error) not in seen:
        seen.add(id(error))

        if isinstance(error, (ssl.SSLError, requests.exceptions.SSLError)):
            return True

        error = error.__cause__ or error.__context__

    return False


def _network_error(error: BaseException, action: str) -> DriveNetworkError:
    """
    Error de red controlado. Solo informa el tipo de fallo: nunca
    el texto de la excepción original, que puede incluir datos de
    la solicitud.
    """
    if _is_certificate_error(error):
        return DriveNetworkError(
            f"No se pudo {action}: falló la verificación del "
            "certificado SSL de Google. Revise la red (por ejemplo, un "
            "proxy que intercepta HTTPS). La verificación no se desactiva."
        )

    if isinstance(error, (requests.exceptions.Timeout, TimeoutError)):
        return DriveNetworkError(
            f"No se pudo {action}: se agotó el tiempo de espera al "
            "contactar a Google."
        )

    return DriveNetworkError(
        f"No se pudo {action} por un error de red "
        f"({type(error).__name__})."
    )


def load_token(path: Path):
    """Credenciales guardadas, o None si no hay token."""

    path = Path(path)

    if not path.exists():
        return None

    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        return Credentials.from_authorized_user_info(info)
    except (OSError, UnicodeDecodeError, ValueError):
        # Sin detalles: el contenido del archivo es secreto.
        raise DriveAuthError(
            f"El token guardado en {path} no es válido. "
            f"Vuelva a autorizar con: {AUTH_COMMAND}"
        ) from None


def save_token(credentials, path: Path) -> None:
    """
    Guarda el token de forma atómica en la carpeta de secretos.

    El temporal se crea con permisos restrictivos (mkstemp) en la
    misma carpeta y reemplaza al anterior solo si se escribió
    completo. El token pertenece a la aplicación, por eso aquí sí
    se usa os.replace().
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if os.name != "nt":
        os.chmod(path.parent, 0o700)

    data = credentials.to_json().encode("utf-8")

    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )

    try:
        with os.fdopen(fd, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())

        os.replace(tmp_name, path)

    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _preserve_damaged_token(path: Path) -> Path:
    """
    Aparta, sin modificarlo, un token dañado antes de guardar uno
    nuevo. Queda en la misma carpeta privada como
    token.damaged-<fecha>-<aleatorio>.json.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(
        f"{path.stem}.damaged-{stamp}-{secrets.token_hex(4)}{path.suffix}"
    )

    try:
        os.rename(path, backup)
    except OSError:
        raise DriveAuthError(
            f"La autorización se completó, pero no se pudo conservar el "
            f"token dañado {path}, así que no se reemplazó. Muévalo "
            f"manualmente y vuelva a ejecutar: {AUTH_COMMAND}"
        ) from None

    return backup


def _authorize(paths: SecretPaths, flow_factory):
    """
    Flujo interactivo en el navegador. No se reintenta: cualquier
    fallo se traduce a un error controlado, sin traceback ni datos
    sensibles.
    """
    if not paths.client_secret.exists():
        raise DriveAuthError(
            "No se encontró el cliente OAuth en "
            f"{paths.client_secret}. Descárguelo desde Google Cloud "
            "Console (tipo: aplicación de escritorio) y guárdelo con "
            "ese nombre."
        )

    invalid_client = DriveAuthError(
        f"El cliente OAuth {paths.client_secret} no es válido. "
        "Debe ser un cliente de tipo aplicación de escritorio."
    )

    try:
        flow = flow_factory(str(paths.client_secret), list(SCOPES))
    except (OSError, ValueError, KeyError):
        raise invalid_client from None

    try:
        credentials = flow.run_local_server(
            port=0,
            open_browser=True,
            timeout_seconds=LOGIN_TIMEOUT_SECONDS,
            authorization_prompt_message=(
                "Abra esta dirección para autorizar el acceso de solo "
                "lectura a Google Drive:\n{url}"
            ),
            success_message=(
                "Autorización completada. Puede cerrar esta ventana."
            ),
        )

    # Red: intercambio del código por tokens (requests) y su SSL.
    # Van primero: las excepciones de requests heredan de OSError y
    # SSLCertVerificationError hereda de ValueError.
    except (
        requests.exceptions.RequestException,
        TransportError,
        ssl.SSLError,
        TimeoutError,
        ConnectionError,
    ) as exc:
        raise _network_error(exc, "completar la autorización") from None

    except WSGITimeoutError:
        raise DriveAuthError(
            "La autorización no se completó en el navegador dentro del "
            f"tiempo límite ({LOGIN_TIMEOUT_SECONDS} segundos)."
        ) from None

    except webbrowser.Error:
        raise DriveAuthError(
            "No se encontró un navegador para autorizar la cuenta. "
            "Ejecute auth en un equipo con navegador."
        ) from None

    except Warning:
        # oauthlib avisa así que se concedió otro scope; la excepción
        # incluye los tokens recibidos, por eso no se muestra.
        raise DriveAuthError(
            "La cuenta no concedió el permiso de solo lectura de Drive."
        ) from None

    except KeyError:
        raise invalid_client from None

    except (OAuth2Error, ValueError, AttributeError):
        raise DriveAuthError(
            "La autorización fue cancelada, rechazada o no se completó."
        ) from None

    except OSError as exc:
        # Por ejemplo, no se pudo abrir el servidor local de retorno.
        raise _network_error(exc, "completar la autorización") from None

    if READONLY_SCOPE not in _granted_scopes(credentials):
        raise DriveAuthError(
            "La cuenta no concedió el permiso de solo lectura de Drive."
        )

    return credentials


def get_credentials(
    paths: SecretPaths,
    interactive: bool = False,
    *,
    flow_factory=InstalledAppFlow.from_client_secrets_file,
    request_factory=Request,
    notices: list[str] | None = None,
):
    """
    Credenciales válidas para Drive.

    1. Reutiliza el token guardado si es válido.
    2. Si expiró y tiene token de actualización, lo renueva y lo
       guarda.
    3. Si no hay token, fue revocado, está dañado o no tiene el
       scope, solo se vuelve a autorizar en modo interactivo
       (comando auth); si no, lanza DriveAuthError. Nunca se
       reintenta solo.

    Un token dañado nunca se trata como válido ni se borra: tras
    una nueva autorización correcta se aparta con otro nombre.
    notices recibe los avisos para el usuario.
    """
    damaged = False

    try:
        credentials = load_token(paths.token)
    except DriveAuthError:
        if not interactive:
            raise
        credentials = None
        damaged = True

    if credentials is not None and READONLY_SCOPE not in _granted_scopes(
        credentials
    ):
        if not interactive:
            raise DriveAuthError(
                "El token guardado no tiene el permiso de solo lectura "
                f"de Drive. Vuelva a autorizar con: {AUTH_COMMAND}"
            )
        credentials = None

    if credentials is not None and credentials.valid:
        return credentials

    if credentials is not None and credentials.refresh_token:
        try:
            credentials.refresh(request_factory())
        except RefreshError:
            if not interactive:
                raise DriveAuthError(
                    "La autorización expiró o fue revocada. "
                    f"Vuelva a autorizar con: {AUTH_COMMAND}"
                ) from None
            credentials = None
        except TransportError as exc:
            raise _network_error(exc, "renovar la autorización") from None
        else:
            save_token(credentials, paths.token)
            return credentials

    if not interactive:
        raise DriveAuthError(
            "No hay una autorización válida guardada. "
            f"Autorice con: {AUTH_COMMAND}"
        )

    credentials = _authorize(paths, flow_factory)

    if damaged:
        backup = _preserve_damaged_token(paths.token)

        if notices is not None:
            notices.append(
                "El token anterior estaba dañado; se conservó sin "
                f"cambios como {backup.name}. Puede eliminarlo si ya "
                "no lo necesita."
            )

    save_token(credentials, paths.token)

    return credentials


def logout(paths: SecretPaths) -> bool:
    """
    Elimina el token local. Devuelve False si no existía.

    El acceso también puede revocarse desde la cuenta de Google
    (Seguridad > Aplicaciones de terceros).
    """
    try:
        paths.token.unlink()
    except FileNotFoundError:
        return False

    return True
