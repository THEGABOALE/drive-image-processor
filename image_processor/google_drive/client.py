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


def _backoff(attempt: int) -> float:
    return min(BASE_DELAY_SECONDS * 2 ** (attempt - 1), MAX_DELAY_SECONDS)


def execute(request, *, attempts: int = MAX_ATTEMPTS, sleep=time.sleep):
    """Ejecuta una solicitud de la API con la política de errores."""

    for attempt in range(1, attempts + 1):
        delay = _backoff(attempt)

        try:
            return request.execute()

        except HttpError as exc:
            status = exc.status_code
            reasons = _reasons(exc)
            rate_limited = status == 429 or (
                status == 403 and reasons & RATE_LIMIT_REASONS
            )

            if status == 401:
                raise DriveAuthError(
                    "Google rechazó la autorización (401). Vuelva a "
                    "autorizar con: python -m image_processor.drive_cli auth"
                ) from None

            if not rate_limited and status not in TRANSIENT_STATUSES:
                detail = f": {', '.join(sorted(reasons))}" if reasons else ""

                if status == 403:
                    raise DriveAccessError(
                        f"Google Drive denegó el acceso (403{detail})."
                    ) from None

                if status == 404:
                    raise DriveNotFoundError(
                        "El archivo o la carpeta no existe o no es "
                        "accesible para esta cuenta (404)."
                    ) from None

                raise DriveError(
                    f"Google Drive respondió con un error ({status}{detail})."
                ) from None

            if rate_limited:
                failure = DriveRateLimitError(
                    f"Google Drive limitó las solicitudes ({status}). "
                    "Inténtelo de nuevo más tarde."
                )
            else:
                failure = DriveTransientError(
                    f"Google Drive no está disponible temporalmente ({status})."
                )

            delay = _retry_after(exc) or delay

        except RefreshError:
            raise DriveAuthError(
                "La autorización expiró o fue revocada. Vuelva a "
                "autorizar con: python -m image_processor.drive_cli auth"
            ) from None

        except ssl.SSLCertVerificationError:
            raise DriveNetworkError(
                "No se pudo verificar el certificado SSL de Google. "
                "Revise la red (por ejemplo, un proxy que intercepta "
                "HTTPS). La verificación no se desactiva."
            ) from None

        except (
            TimeoutError,
            ConnectionError,
            httplib2.HttpLib2Error,
            TransportError,
            OSError,
        ) as exc:
            failure = DriveNetworkError(
                "No se pudo comunicar con Google Drive "
                f"({type(exc).__name__})."
            )

        logger.debug(
            "Intento %d de %d fallido: %s", attempt, attempts, failure
        )

        if attempt == attempts:
            raise failure

        sleep(delay)

    raise AssertionError("inalcanzable")
