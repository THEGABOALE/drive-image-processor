"""
Cliente de Google Drive API v3 y ejecución de solicitudes.

execute() clasifica las respuestas:

- 401 o credenciales revocadas: DriveAuthError, sin reintentos.
- 403 por permisos: DriveAccessError, sin reintentos.
- 404: DriveNotFoundError.
- 429, 403 por cuota y 5xx: reintentos acotados con espera
  exponencial (o la indicada por Retry-After); luego
  DriveRateLimitError o DriveTransientError.
- Fallos de red: los mismos reintentos; luego DriveNetworkError.
  Un certificado SSL inválido no se reintenta y la verificación
  nunca se desactiva.
"""

import logging
import ssl
import time
from dataclasses import dataclass

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .errors import (
    DriveAccessError,
    DriveAuthError,
    DriveError,
    DriveNetworkError,
    DriveNotFoundError,
    DriveRateLimitError,
    DriveTransientError,
)


logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 32.0

TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}


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

        return TranslatedError(error, retryable=True, retry_after=_retry_after(exc))

    if isinstance(exc, RefreshError):
        return TranslatedError(
            DriveAuthError(
                "La autorización expiró o fue revocada. Vuelva a "
                "autorizar con: python -m image_processor.drive_cli auth"
            ),
            retryable=False,
        )

    if isinstance(exc, ssl.SSLCertVerificationError):
        return TranslatedError(
            DriveNetworkError(
                "No se pudo verificar el certificado SSL de Google. "
                "Revise la red (por ejemplo, un proxy que intercepta "
                "HTTPS). La verificación no se desactiva."
            ),
            retryable=False,
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
