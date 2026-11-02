"""
Conexión de una cuenta de Google con el cliente OAuth de escritorio.

Se reutiliza el flujo de aplicaciones instaladas del motor (navegador del
sistema y redirección loopback) con dos autorizaciones separadas, como los
perfiles auth.READ y auth.WRITE: una para leer y otra para crear. La
aplicación tiene sus propios tokens; los de la CLI no se leen ni se cambian.

Cualquier cuenta puede conectarse: no hay lista de usuarios. Solo se exige
que ambas autorizaciones sean de la misma cuenta.

Identidad
---------

Cada autorización agrega el permiso openid, que solo permite obtener el
identificador estable de la cuenta (sub) en un ID token firmado por Google.
No se pide email ni profile: el correo que se muestra viene de la API de
Drive y nunca se usa para decidir nada. La firma, el emisor, la audiencia
(este cliente) y la vigencia se verifican; sin un sub verificado no se
guarda nada.

Desconectar
-----------

Solo borra los tokens locales. Revocarlos en Google quitaría todos los
permisos del proyecto para esa cuenta, incluidos los de la CLI.
"""

import functools
import logging
import os
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2 import id_token as google_id_token
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from image_processor.google_drive import auth, client, queries
from image_processor.google_drive.errors import DriveAuthError, DriveError

from .token_store import TokenStoreError


logger = logging.getLogger(__name__)

OPENID_SCOPE = "openid"

# Tolerancia al validar el ID token: el reloj del equipo puede estar corrido.
CLOCK_SKEW_SECONDS = 300

STORE_VERSION = 1

_READ_PROFILE = auth.AuthProfile(
    scopes=(*auth.READ.scopes, OPENID_SCOPE),
    token_attr="token",
    command="",
    permission="el permiso de solo lectura de Drive",
    prompt="Abra esta dirección para autorizar la lectura de Google Drive:\n{url}",
    exact=True,
)

_WRITE_PROFILE = auth.AuthProfile(
    scopes=(*auth.WRITE.scopes, OPENID_SCOPE),
    token_attr="write_token",
    command="",
    permission="exactamente el permiso drive.file (crear archivos propios)",
    prompt="Abra esta dirección para autorizar la creación de archivos propios:\n{url}",
    exact=True,
)


class State(str, Enum):
    DISCONNECTED = "disconnected"
    AUTHORIZING_READ = "authorizing_read"
    AUTHORIZING_WRITE = "authorizing_write"
    CONNECTED = "connected"
    RECONNECT_REQUIRED = "reconnect_required"


_AUTHORIZING = (State.AUTHORIZING_READ, State.AUTHORIZING_WRITE)


class NotConnectedError(Exception):
    """No hay una cuenta conectada."""


class AlreadyConnectedError(ValueError):
    """Ya hay una cuenta conectada o una autorización en curso."""


class IdentityError(Exception):
    """No se pudo verificar la identidad de la cuenta. Nunca incluye datos."""


class AccountMismatchError(Exception):
    """Las dos autorizaciones son de cuentas distintas."""


@dataclass(frozen=True)
class Account:
    sub: str
    email: str | None
    display_name: str | None


@dataclass(frozen=True)
class ConnectionStatus:
    state: State
    account: Account | None = None
    message: str | None = None
    error: str | None = None


def verify_google_id_token(token: str, audience: str, *, request=None) -> dict:
    """Las afirmaciones del ID token, tras verificar su firma y vigencia."""

    try:
        return google_id_token.verify_oauth2_token(
            token,
            request or Request(),
            audience=audience,
            clock_skew_in_seconds=CLOCK_SKEW_SECONDS,
        )
    except (ValueError, GoogleAuthError):
        raise IdentityError("El ID token de Google no es válido.") from None


class _LoginHintFlow(InstalledAppFlow):
    login_hint: str | None = None

    def authorization_url(self, **kwargs):
        if self.login_hint:
            kwargs.setdefault("login_hint", self.login_hint)
        return super().authorization_url(**kwargs)


def default_flow_factory(client_file, scopes, login_hint=None):
    flow = _LoginHintFlow.from_client_secrets_file(client_file, scopes)
    flow.login_hint = login_hint
    return flow


def _start_daemon_thread(target) -> None:
    threading.Thread(target=target, daemon=True, name="google-connect").start()


class ConnectionManager:
    """
    La conexión actual y la autorización en curso.

    Cada intento tiene un número de generación: cancelar lo cambia, y el
    resultado de un intento anterior se descarta aunque termine más tarde
    (por ejemplo, si el usuario completa una pestaña que había abandonado).
    Solo un intento completo reemplaza los datos guardados.
    """

    def __init__(
        self,
        settings,
        store,
        *,
        flow_factory=default_flow_factory,
        verify_id_token=verify_google_id_token,
        service_factory=client.build_service,
        start_thread=_start_daemon_thread,
    ):
        self._settings = settings
        self._store = store
        self._flow_factory = flow_factory
        self._verify_id_token = verify_id_token
        self._service_factory = service_factory
        self._start_thread = start_thread
        self._lock = threading.RLock()
        self._generation = 0
        self._bundle = None
        self._expired = False
        self._message = None
        self._error = None
        self._restore()
        self._state = self._resting_state()

    def _restore(self) -> None:
        try:
            bundle = self._store.load()
        except TokenStoreError as exc:
            self._expired = True
            self._message = str(exc)
            return

        if bundle is None:
            return

        if (
            bundle.get("version") != STORE_VERSION
            or bundle.get("client_id") != self._settings.desktop_client_id
        ):
            # Datos de otro cliente o de un formato anterior: no se reutilizan.
            self._expired = True
            self._message = "Hay que volver a conectar la cuenta."
            return

        self._bundle = bundle

    def _resting_state(self) -> State:
        if self._expired:
            return State.RECONNECT_REQUIRED
        return State.CONNECTED if self._bundle else State.DISCONNECTED

    def status(self) -> ConnectionStatus:
        with self._lock:
            bundle = self._bundle
            account = bundle and Account(
                bundle["sub"], bundle.get("email"), bundle.get("display_name")
            )
            return ConnectionStatus(
                self._state, account or None, self._message, self._error
            )

    def start(self) -> None:
        """Inicia la autorización en el navegador del sistema, en otro hilo."""

        with self._lock:
            if self._state is State.CONNECTED or self._state in _AUTHORIZING:
                raise AlreadyConnectedError(self._state.value)

            self._generation += 1
            generation = self._generation
            self._state = State.AUTHORIZING_READ
            self._message = None
            self._error = None

        self._start_thread(lambda: self._run(generation))

    def cancel(self) -> None:
        """
        Descarta la autorización en curso. El servidor local de retorno de
        ese intento termina solo al agotarse su tiempo límite.
        """
        with self._lock:
            if self._state in _AUTHORIZING:
                self._generation += 1
                self._state = self._resting_state()
                self._message = None
                self._error = None

    def disconnect(self) -> None:
        """Borra los tokens locales. No toca nada en Google ni en la CLI."""

        with self._lock:
            self._generation += 1
            self._store.clear()
            self._bundle = None
            self._expired = False
            self._state = State.DISCONNECTED
            self._message = None
            self._error = None

    def mark_expired(self) -> None:
        """Google rechazó la autorización guardada: hay que reconectar."""

        with self._lock:
            self._expired = True
            if self._state is State.CONNECTED:
                self._state = State.RECONNECT_REQUIRED

    def services(self):
        """
        Servicios de Drive de lectura y escritura. Se construyen en cada
        llamada: no son seguros entre hilos.
        """
        with self._lock:
            if self._state is not State.CONNECTED:
                raise NotConnectedError()
            bundle = self._bundle

        return (
            self._service_factory(
                self._credentials(bundle["read_refresh_token"], _READ_PROFILE)
            ),
            self._service_factory(
                self._credentials(bundle["write_refresh_token"], _WRITE_PROFILE)
            ),
        )

    def _credentials(self, refresh_token: str, profile) -> Credentials:
        desktop = self._settings.desktop_client
        return Credentials(
            None,
            refresh_token=refresh_token,
            token_uri=desktop["token_uri"],
            client_id=desktop["client_id"],
            client_secret=desktop["client_secret"],
            scopes=list(profile.scopes),
        )

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def _run(self, generation: int) -> None:
        try:
            bundle = self._authorize_both(generation)
        except AccountMismatchError:
            self._fail(
                generation,
                "account_mismatch",
                "Las dos autorizaciones deben ser de la misma cuenta de Google. "
                "No se guardó nada: vuelva a conectar eligiendo una sola cuenta.",
            )
        except IdentityError:
            self._fail(
                generation,
                "identity_unverified",
                "No se pudo verificar la identidad de la cuenta con Google. "
                "No se guardó nada.",
            )
        except DriveError as exc:
            # Los mensajes del motor nunca incluyen tokens.
            self._fail(generation, "authorization_failed", str(exc))
        except (TokenStoreError, OSError) as exc:
            logger.error("No se pudo guardar la autorización (%s).", type(exc).__name__)
            self._fail(
                generation, "storage_error", "No se pudo guardar la autorización."
            )
        except Exception as exc:
            # Solo el tipo: el texto podría incluir datos de la solicitud.
            logger.error("Fallo inesperado al conectar (%s).", type(exc).__name__)
            self._fail(
                generation, "unexpected_error", "No se pudo completar la conexión."
            )
        else:
            if bundle is not None:
                self._finish(generation, bundle)

    def _authorize_both(self, generation: int) -> dict | None:
        read = self._authorize(_READ_PROFILE, None)
        sub = self._verified_sub(read)

        with self._lock:
            if generation != self._generation:
                return None
            self._state = State.AUTHORIZING_WRITE

        write = self._authorize(_WRITE_PROFILE, sub)

        if not self._is_current(generation):
            return None

        if self._verified_sub(write) != sub:
            raise AccountMismatchError()

        account = self._display_data(read)

        return {
            "version": STORE_VERSION,
            "client_id": self._settings.desktop_client_id,
            "sub": sub,
            "email": account.email,
            "display_name": account.display_name,
            "read_refresh_token": read.refresh_token,
            "write_refresh_token": write.refresh_token,
        }

    def _authorize(self, profile, login_hint: str | None):
        settings = self._settings
        paths = auth.SecretPaths(
            directory=Path(settings.desktop_client_file).parent,
            client_secret=Path(settings.desktop_client_file),
            # El flujo nunca usa estas rutas: los tokens van al almacenamiento.
            token=Path(os.devnull),
            write_token=Path(os.devnull),
        )
        credentials = auth._authorize(
            paths,
            lambda client_file, scopes: self._flow_factory(
                client_file, scopes, login_hint
            ),
            profile,
        )

        if not credentials.refresh_token:
            raise DriveAuthError(
                "Google no entregó una autorización que se pueda renovar."
            )

        return credentials

    def _verified_sub(self, credentials) -> str:
        if not credentials.id_token:
            raise IdentityError("Google no entregó un ID token.")

        claims = self._verify_id_token(
            credentials.id_token, self._settings.desktop_client_id
        )
        sub = claims.get("sub")

        if not isinstance(sub, str) or not sub:
            raise IdentityError("El ID token no identifica a la cuenta.")

        return sub

    def _display_data(self, read_credentials) -> queries.DriveAccount:
        """El nombre y el correo solo se muestran: un fallo no impide conectar."""

        try:
            return queries.verify_connection(
                self._service_factory(read_credentials),
                execute=functools.partial(client.execute, attempts=1),
            )
        except Exception as exc:
            logger.info("Sin datos de la cuenta para mostrar (%s).", type(exc).__name__)
            return queries.DriveAccount(None, None)

    def _finish(self, generation: int, bundle: dict) -> None:
        with self._lock:
            if generation != self._generation:
                return

            try:
                self._store.save(bundle)
            except (TokenStoreError, OSError) as exc:
                logger.error(
                    "No se pudo guardar la autorización (%s).", type(exc).__name__
                )
                self._error = "storage_error"
                self._message = "No se pudo guardar la autorización."
                self._state = self._resting_state()
                return

            self._bundle = bundle
            self._expired = False
            self._state = State.CONNECTED
            self._error = None
            self._message = None

    def _fail(self, generation: int, code: str, message: str) -> None:
        with self._lock:
            if generation != self._generation:
                return

            self._error = code
            self._message = message
            self._state = self._resting_state()
