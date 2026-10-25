"""
OAuth 2.0 para una aplicación local de escritorio.

Dos autorizaciones independientes, cada una con su token:

- Lectura (READ, comando auth): drive.readonly, en token.json. Es un
  scope restringido de Google: sirve para esta prueba local con una
  cuenta autorizada como usuario de prueba, pero una aplicación
  pública que lo use puede requerir la verificación de Google.
- Escritura (WRITE, comando auth-write, GD-03B): exactamente
  drive.file, en token_write.json. Es un scope no sensible: solo da
  acceso a los archivos que crea la aplicación y a los que el usuario
  le comparte (por ejemplo, con Google Picker). Un token con permisos
  más amplios (drive) se rechaza: nunca se usa como alternativa.

Autorizar la escritura nunca lee, modifica ni reemplaza token.json.

Los secretos se guardan en una carpeta privada configurable
(por defecto .secrets/, excluida de Git):

- credentials.json: cliente OAuth de tipo "aplicación de
  escritorio", descargado de Google Cloud Console.
- token.json y token_write.json: tokens de acceso y de
  actualización de la cuenta.

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

WRITE_SCOPE = "https://www.googleapis.com/auth/drive.file"
WRITE_SCOPES = (WRITE_SCOPE,)

SECRETS_DIR_ENV = "DRIVE_IMAGE_PROCESSOR_SECRETS_DIR"
DEFAULT_SECRETS_DIR = Path(".secrets")

CLIENT_SECRET_NAME = "credentials.json"
TOKEN_NAME = "token.json"
WRITE_TOKEN_NAME = "token_write.json"

# Tiempo máximo para completar la autorización en el navegador.
LOGIN_TIMEOUT_SECONDS = 300

AUTH_COMMAND = "python -m image_processor.drive_cli auth"
AUTH_WRITE_COMMAND = "python -m image_processor.drive_cli auth-write"


@dataclass(frozen=True)
class SecretPaths:
    directory: Path
    client_secret: Path
    token: Path
    write_token: Path


@dataclass(frozen=True)
class AuthProfile:
    """
    Una autorización: sus scopes, su token y cómo se renueva.

    exact exige que el token tenga exactamente esos scopes (ni más
    amplios ni adicionales); si no, basta con que los incluya.
    """

    scopes: tuple[str, ...]
    token_attr: str
    command: str
    permission: str
    prompt: str
    exact: bool = False

    def token_path(self, paths: SecretPaths) -> Path:
        return getattr(paths, self.token_attr)

    def accepts(self, granted: set[str]) -> bool:
        if self.exact:
            return granted == set(self.scopes)
        return set(self.scopes) <= granted


READ = AuthProfile(
    scopes=SCOPES,
    token_attr="token",
    command=AUTH_COMMAND,
    permission="el permiso de solo lectura de Drive",
    prompt=(
        "Abra esta dirección para autorizar el acceso de solo "
        "lectura a Google Drive:\n{url}"
    ),
)

WRITE = AuthProfile(
    scopes=WRITE_SCOPES,
    token_attr="write_token",
    command=AUTH_WRITE_COMMAND,
    permission=(
        "exactamente el permiso drive.file (crear archivos propios); "
        "no se usan permisos más amplios"
    ),
    prompt=(
        "Abra esta dirección para autorizar que la aplicación cree "
        "archivos propios en Google Drive (drive.file):\n{url}"
    ),
    exact=True,
)


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
        write_token=directory / WRITE_TOKEN_NAME,
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


def load_token(path: Path, command: str = AUTH_COMMAND):
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
            f"Vuelva a autorizar con: {command}"
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


def _preserve_damaged_token(path: Path, command: str = AUTH_COMMAND) -> Path:
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
            f"manualmente y vuelva a ejecutar: {command}"
        ) from None

    return backup


def _authorize(paths: SecretPaths, flow_factory, profile: AuthProfile = READ):
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
        flow = flow_factory(str(paths.client_secret), list(profile.scopes))
    except (OSError, ValueError, KeyError):
        raise invalid_client from None

    try:
        credentials = flow.run_local_server(
            port=0,
            open_browser=True,
            timeout_seconds=LOGIN_TIMEOUT_SECONDS,
            authorization_prompt_message=profile.prompt,
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
            f"La cuenta no concedió {profile.permission}."
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

    if not profile.accepts(_granted_scopes(credentials)):
        raise DriveAuthError(f"La cuenta no concedió {profile.permission}.")

    return credentials


def get_credentials(
    paths: SecretPaths,
    interactive: bool = False,
    *,
    flow_factory=InstalledAppFlow.from_client_secrets_file,
    request_factory=Request,
    notices: list[str] | None = None,
    profile: AuthProfile = READ,
    persist: bool = True,
):
    """
    Credenciales válidas para Drive con la autorización profile
    (READ por defecto, o WRITE). Cada una usa solo su token.

    persist=False renueva el token solo en memoria: el archivo no
    cambia (vista previa).

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
    token_path = profile.token_path(paths)
    command = profile.command

    try:
        credentials = load_token(token_path, command)
    except DriveAuthError:
        if not interactive:
            raise
        credentials = None
        damaged = True

    if credentials is not None and not profile.accepts(
        _granted_scopes(credentials)
    ):
        if not interactive:
            raise DriveAuthError(
                f"El token guardado no tiene {profile.permission}. "
                f"Vuelva a autorizar con: {command}"
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
                    f"Vuelva a autorizar con: {command}"
                ) from None
            credentials = None
        except TransportError as exc:
            raise _network_error(exc, "renovar la autorización") from None
        else:
            if persist:
                save_token(credentials, token_path)
            return credentials

    if not interactive:
        raise DriveAuthError(
            "No hay una autorización válida guardada. "
            f"Autorice con: {command}"
        )

    credentials = _authorize(paths, flow_factory, profile)

    if damaged:
        backup = _preserve_damaged_token(token_path, command)

        if notices is not None:
            notices.append(
                "El token anterior estaba dañado; se conservó sin "
                f"cambios como {backup.name}. Puede eliminarlo si ya "
                "no lo necesita."
            )

    save_token(credentials, token_path)

    return credentials


def logout(paths: SecretPaths, profile: AuthProfile = READ) -> bool:
    """
    Elimina el token local de esa autorización (solo ese). Devuelve
    False si no existía.

    El acceso también puede revocarse desde la cuenta de Google
    (Seguridad > Aplicaciones de terceros).
    """
    try:
        profile.token_path(paths).unlink()
    except FileNotFoundError:
        return False

    return True
