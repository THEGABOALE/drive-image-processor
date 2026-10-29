"""
Selección de carpetas existentes con Google Picker.

Se usa la variante oficial de Google Picker para aplicaciones de
escritorio: la misma autorización OAuth 2.0 de escritorio de auth-write,
con los parámetros de Picker (trigger_onepick=true, prompt=consent,
allow_folder_selection=true y solo el tipo carpeta). Google la muestra
en el navegador del sistema y redirige a la URI local de retorno con
picked_file_ids, o con error si el usuario cancela. La interfaz es la de
Google: no hay página propia ni clave de API.

Permisos
--------

El Picker de escritorio exige exactamente drive.file, el permiso de la
autorización de escritura: no se amplía nada. Elegir una carpeta le da a
la aplicación acceso drive.file a ella, así que puede crear los PNG
dentro (destino). La lectura de los originales sigue usando
drive.readonly; token.json no se modifica.

El token que entrega el Picker se valida (exactamente drive.file) y,
salvo en la vista previa, reemplaza a token_write.json, como auth-write.

Verificación
------------

Lo elegido se comprueba con la API de Drive antes de usarlo: el origen
debe ser una carpeta existente y legible, y el destino una carpeta en la
que drive.file puede crear archivos. Una cancelación, un tiempo agotado,
una selección que no es una sola carpeta o un error de autorización
nunca dejan empezar una sincronización.
"""

import ssl
import webbrowser
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import requests
from google.auth.exceptions import TransportError
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
from oauthlib.oauth2.rfc6749.errors import OAuth2Error

from . import auth, queries, uploads
from .errors import DriveAuthError


# Parámetros de Google Picker para aplicaciones de escritorio.
PICKER_PARAMS = {
    "prompt": "consent",
    "trigger_onepick": "true",
    "allow_folder_selection": "true",
    "mimetypes": queries.FOLDER_MIME,
}


class PickerCancelled(Exception):
    """No se eligió ninguna carpeta (cancelación o tiempo agotado)."""


class PickerSelectionError(ValueError):
    """Google Picker no devolvió exactamente una carpeta válida."""


@dataclass(frozen=True)
class PickedFolder:
    id: str
    # Autorización drive.file que entregó el Picker (no se muestra nunca).
    credentials: object


@dataclass(frozen=True)
class FolderSelection:
    source: queries.DriveFolder
    target: uploads.TargetAccess

    @property
    def same_folder(self) -> bool:
        return self.source.id == self.target.folder.id


def picked_file_ids(authorization_response: str) -> list[str]:
    """
    IDs que Google Picker agregó a la redirección. Una redirección con
    error (cancelación) o sin IDs no elige nada.
    """
    params = parse_qs(urlsplit(authorization_response).query)

    if "error" in params:
        raise PickerCancelled(
            "Se canceló la selección en Google Picker; no se eligió ninguna carpeta."
        )

    raw = (params.get("picked_file_ids") or [""])[0]
    ids = [part for part in raw.split(",") if part]

    if not ids:
        raise PickerCancelled("No se eligió ninguna carpeta en Google Picker.")

    return ids


class PickerFlow(InstalledAppFlow):
    """
    Autorización de escritorio que además recoge los IDs elegidos en
    Google Picker. Una cancelación se detecta antes de pedir tokens.
    """

    picked_ids: list[str] | None = None

    def fetch_token(self, **kwargs):
        response = kwargs.get("authorization_response")

        if response is not None:
            self.picked_ids = picked_file_ids(response)

        return super().fetch_token(**kwargs)


def _store_write_token(paths, credentials) -> None:
    """Como auth-write: un token de escritura dañado se aparta, no se borra."""

    try:
        auth.load_token(paths.write_token, auth.AUTH_WRITE_COMMAND)
    except DriveAuthError:
        auth._preserve_damaged_token(paths.write_token, auth.AUTH_WRITE_COMMAND)

    auth.save_token(credentials, paths.write_token)


def pick_folder(
    paths,
    purpose: str,
    *,
    persist: bool = True,
    flow_factory=PickerFlow.from_client_secrets_file,
) -> PickedFolder:
    """
    Abre Google Picker para elegir una carpeta existente.

    purpose describe la carpeta en el mensaje (por ejemplo, "la carpeta
    de origen"). persist=False no guarda la autorización (vista previa).
    Lanza PickerCancelled, PickerSelectionError, DriveAuthError o un
    error de red controlado.
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
        flow = flow_factory(str(paths.client_secret), list(auth.WRITE.scopes))
    except (OSError, ValueError, KeyError):
        raise invalid_client from None

    try:
        credentials = flow.run_local_server(
            port=0,
            open_browser=True,
            timeout_seconds=auth.LOGIN_TIMEOUT_SECONDS,
            authorization_prompt_message=(
                f"Abra esta dirección para elegir {purpose} en Google Picker:\n{{url}}"
            ),
            success_message="Selección recibida. Puede cerrar esta ventana.",
            **PICKER_PARAMS,
        )

    except PickerCancelled:
        raise

    # Red y SSL primero: las excepciones de requests heredan de OSError.
    except (
        requests.exceptions.RequestException,
        TransportError,
        ssl.SSLError,
        TimeoutError,
        ConnectionError,
    ) as exc:
        raise auth._network_error(exc, "completar la selección en Google Picker") from None

    except WSGITimeoutError:
        raise PickerCancelled(
            "La selección no se completó en el navegador dentro del tiempo "
            f"límite ({auth.LOGIN_TIMEOUT_SECONDS} segundos)."
        ) from None

    except webbrowser.Error:
        raise DriveAuthError(
            "No se encontró un navegador para abrir Google Picker."
        ) from None

    except Warning:
        # oauthlib avisa así que se concedió otro scope; la excepción
        # incluye los tokens recibidos, por eso no se muestra.
        raise DriveAuthError(f"La cuenta no concedió {auth.WRITE.permission}.") from None

    except KeyError:
        raise invalid_client from None

    except (OAuth2Error, ValueError, AttributeError):
        raise DriveAuthError(
            "La autorización de Google Picker fue rechazada o no se completó."
        ) from None

    except OSError as exc:
        raise auth._network_error(exc, "completar la selección en Google Picker") from None

    if not auth.WRITE.accepts(auth._granted_scopes(credentials)):
        raise DriveAuthError(f"La cuenta no concedió {auth.WRITE.permission}.")

    ids = flow.picked_ids or []

    if len(ids) != 1:
        raise PickerSelectionError(
            f"Google Picker devolvió {len(ids)} elementos; elija una sola carpeta."
        )

    try:
        folder_id = queries.validate_file_id(ids[0])
    except ValueError:
        raise PickerSelectionError(
            "Google Picker devolvió un identificador inesperado."
        ) from None

    if persist:
        _store_write_token(paths, credentials)

    return PickedFolder(folder_id, credentials)


def verify_selection(
    read_service,
    write_service,
    source_id: str,
    target_id: str,
    *,
    dry_run: bool = False,
    execute=None,
) -> FolderSelection:
    """
    Comprueba con la API de Drive que el origen sea una carpeta existente
    y legible (drive.readonly) y el destino una carpeta en la que
    drive.file puede crear archivos (en la vista previa, solo se informa).
    Solo consulta: no crea ni modifica nada.
    """
    source = queries.get_folder(read_service, source_id, execute=execute)
    target = uploads.check_target(
        read_service, write_service, target_id, dry_run=dry_run, execute=execute
    )
    return FolderSelection(source, target)
