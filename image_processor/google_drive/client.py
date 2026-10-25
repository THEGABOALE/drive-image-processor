"""
Cliente de Google Drive API v3 y ejecución de solicitudes.

execute() clasifica las respuestas:

- 401 o credenciales revocadas: DriveAuthError, sin reintentos.
- 403 por permisos: DriveAccessError, sin reintentos.
- 404: DriveNotFoundError.
- 429, 403 por límite de frecuencia y 5xx: reintentos acotados con
  espera exponencial (o la indicada por Retry-After); luego
  DriveRateLimitError o DriveTransientError.
- 403 por cuota agotada (dailyLimitExceeded, quotaExceeded):
  DriveQuotaError, sin reintentos. No es un error de permisos.
- Fallos de red: los mismos reintentos; luego DriveNetworkError.
- TLS: un certificado inválido (DriveCertificateError) o una
  negociación TLS fallida (DriveSecureConnectionError) no se
  reintentan, aunque lleguen encadenados o envueltos por el
  transporte (TransportError de google-auth, requests o urllib3). La
  verificación nunca se desactiva. Un corte de una conexión TLS ya
  establecida (EOF) sí se reintenta.

Los mensajes nunca incluyen el texto de la excepción original.
"""

import logging
import ssl
import time
from dataclasses import dataclass

import httplib2
import requests
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .errors import (
    DriveAccessError,
    DriveAuthError,
    DriveCertificateError,
    DriveError,
    DriveNetworkError,
    DriveNotFoundError,
    DriveQuotaError,
    DriveRateLimitError,
    DriveSecureConnectionError,
    DriveTransientError,
)


logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 32.0

TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}

# Cuotas agotadas: reintentar enseguida no sirve.
QUOTA_REASONS = {"dailyLimitExceeded", "quotaExceeded"}

# Cortes de una conexión TLS ya establecida: temporales.
_TRANSIENT_TLS_ERRORS = (
    ssl.SSLEOFError,
    ssl.SSLZeroReturnError,
    ssl.SSLSyscallError,
    ssl.SSLWantReadError,
    ssl.SSLWantWriteError,
)

# Límite de excepciones revisadas en una cadena.
_MAX_CHAIN = 32

_CERTIFICATE_MESSAGE = (
    "No se pudo verificar el certificado SSL de Google. Revise la red "
    "(por ejemplo, un proxy que intercepta HTTPS). La verificación no "
    "se desactiva."
)

_HANDSHAKE_MESSAGE = (
    "No se pudo establecer una conexión segura (TLS) con Google. Revise "
    "la red o el proxy. No se reintenta y la verificación no se desactiva."
)


def build_service(credentials):
    """Cliente de Drive v3 con el documento de descubrimiento local."""

    return build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
        static_discovery=True,
    )


def _reasons(error: HttpError) -> set[str]:
    details = getattr(error, "error_details", None)

    if not isinstance(details, list):
        return set()

    return {
        item["reason"]
        for item in details
        if isinstance(item, dict) and isinstance(item.get("reason"), str)
    }


def _retry_after(error: HttpError) -> float | None:
    try:
        value = float(error.resp.get("retry-after"))
    except (AttributeError, TypeError, ValueError):
        return None

    return min(max(value, 0.0), MAX_DELAY_SECONDS)


def _chain(exc: BaseException):
    """
    La excepción y las que la originaron: encadenadas (__cause__,
    __context__), recibidas como argumento (TransportError, requests)
    o como motivo (urllib3 MaxRetryError.reason).
    """
    pending = [exc]
    seen = set()

    while pending and len(seen) < _MAX_CHAIN:
        current = pending.pop(0)

        if id(current) in seen:
            continue

        seen.add(id(current))
        yield current

        linked = [
            current.__cause__,
            current.__context__,
            getattr(current, "reason", None),
            *getattr(current, "args", ()),
        ]
        pending.extend(item for item in linked if isinstance(item, BaseException))


def _secure_connection_failure(exc: BaseException) -> str | None:
    """
    "certificate", "handshake" o None. Solo se examina la cadena; su
    texto nunca se muestra.
    """
    legacy_handshake = getattr(httplib2, "SSLHandshakeError", None)
    handshake = False
    transient = False

    for item in _chain(exc):
        if isinstance(item, ssl.SSLCertVerificationError):
            return "certificate"

        if isinstance(item, _TRANSIENT_TLS_ERRORS):
            transient = True

        elif isinstance(item, ssl.SSLError):
            detail = " ".join(
                str(part) for part in (getattr(item, "reason", None), *item.args)
            )
            if "CERTIFICATE_VERIFY_FAILED" in detail:
                return "certificate"
            handshake = True

        elif isinstance(item, requests.exceptions.SSLError) or (
            legacy_handshake is not None and isinstance(item, legacy_handshake)
        ):
            handshake = True

    if handshake and not transient:
        return "handshake"

    return None


def backoff(attempt: int) -> float:
    """Espera antes del reintento número attempt (1, 2, 4, 8... s)."""

    return min(BASE_DELAY_SECONDS * 2 ** (attempt - 1), MAX_DELAY_SECONDS)


@dataclass(frozen=True)
class TranslatedError:
    error: DriveError
    retryable: bool
    retry_after: float | None = None


def translate_error(exc: BaseException) -> TranslatedError | None:
    """
    Clasifica una excepción de la API o de la red. Devuelve None si
    no corresponde a Drive (se deja propagar sin cambios).
    """
    if isinstance(exc, HttpError):
        status = exc.status_code
        reasons = _reasons(exc)
        rate_limited = status == 429 or (
            status == 403 and bool(reasons & RATE_LIMIT_REASONS)
        )

        if status == 403 and reasons & QUOTA_REASONS:
            error = DriveQuotaError(
                "Se agotó la cuota de la API de Google Drive "
                f"(403: {', '.join(sorted(reasons & QUOTA_REASONS))}). "
                "Inténtelo de nuevo más tarde."
            )
            error.reasons = frozenset(reasons)
            return TranslatedError(error, retryable=False)

        if status == 401:
            return TranslatedError(
                DriveAuthError(
                    "Google rechazó la autorización (401). Vuelva a "
                    "autorizar con: python -m image_processor.drive_cli auth"
                ),
                retryable=False,
            )

        if not rate_limited and status not in TRANSIENT_STATUSES:
            detail = f": {', '.join(sorted(reasons))}" if reasons else ""

            if status == 403:
                error = DriveAccessError(
                    f"Google Drive denegó el acceso (403{detail})."
                )
            elif status == 404:
                error = DriveNotFoundError(
                    "El archivo o la carpeta no existe o no es "
                    "accesible para esta cuenta (404)."
                )
            else:
                error = DriveError(
                    f"Google Drive respondió con un error ({status}{detail})."
                )

            error.reasons = frozenset(reasons)
            return TranslatedError(error, retryable=False)

        if rate_limited:
            error = DriveRateLimitError(
                f"Google Drive limitó las solicitudes ({status}). "
                "Inténtelo de nuevo más tarde."
            )
        else:
            error = DriveTransientError(
                f"Google Drive no está disponible temporalmente ({status})."
            )

        error.reasons = frozenset(reasons)
        return TranslatedError(error, retryable=True, retry_after=_retry_after(exc))

    if isinstance(exc, RefreshError):
        return TranslatedError(
            DriveAuthError(
                "La autorización expiró o fue revocada. Vuelva a "
                "autorizar con: python -m image_processor.drive_cli auth"
            ),
            retryable=False,
        )

    failure = _secure_connection_failure(exc)

    if failure == "certificate":
        return TranslatedError(
            DriveCertificateError(_CERTIFICATE_MESSAGE), retryable=False
        )

    if failure == "handshake":
        return TranslatedError(
            DriveSecureConnectionError(_HANDSHAKE_MESSAGE), retryable=False
        )

    if isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            httplib2.HttpLib2Error,
            TransportError,
            OSError,
        ),
    ):
        return TranslatedError(
            DriveNetworkError(
                "No se pudo comunicar con Google Drive "
                f"({type(exc).__name__})."
            ),
            retryable=True,
        )

    return None


def execute(request, *, attempts: int = MAX_ATTEMPTS, sleep=time.sleep):
    """Ejecuta una solicitud de la API con la política de errores."""

    for attempt in range(1, attempts + 1):
        try:
            return request.execute()
        except Exception as exc:
            translated = translate_error(exc)

            if translated is None:
                raise

            if not translated.retryable:
                raise translated.error from None

            failure = translated.error
            delay = translated.retry_after or backoff(attempt)

        logger.debug(
            "Intento %d de %d fallido: %s", attempt, attempts, failure
        )

        if attempt == attempts:
            raise failure from None

        sleep(delay)

    raise AssertionError("inalcanzable")
