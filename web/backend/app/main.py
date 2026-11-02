"""
API local de la aplicación web.

Protección de las rutas: el host y el origen se validan en cada solicitud
(evita DNS rebinding y lecturas desde otros sitios), no se envían cabeceras
CORS, y toda solicitud que cambia algo necesita un Origin permitido y el
token CSRF de esta ejecución, que solo una página del mismo origen puede leer.
"""

import logging
import secrets

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from image_processor.google_drive import queries
from image_processor.google_drive.errors import (
    DriveAccessError,
    DriveAuthError,
    DriveError,
    DriveNotAFolderError,
    DriveNotFoundError,
    DriveQuotaError,
    DriveSecureConnectionError,
    DriveTransientError,
    DriveWriteAccessError,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .connection import AlreadyConnectedError, ConnectionManager, NotConnectedError
from .folders import SourceNotAuthorizedError, verify_folders


logger = logging.getLogger(__name__)

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Sec-Fetch-Site aceptados. Otro puerto de localhost es "same-site".
_ALLOWED_FETCH_SITES = {None, "same-origin", "none"}

# Solo mensajes propios: los del motor pueden citar comandos de la CLI. El
# orden importa: las subclases van antes.
_FOLDER_ERRORS = (
    (
        SourceNotAuthorizedError, 409, "source_not_authorized",
        "La carpeta de origen no está autorizada para la cuenta conectada. "
        "Elíjala de nuevo con Google Picker usando esa misma cuenta.",
    ),
    (
        DriveWriteAccessError, 409, "target_not_authorized",
        "La cuenta conectada no puede crear archivos en la carpeta de destino. "
        "Elíjala de nuevo con Google Picker usando esa misma cuenta; si es "
        "compartida, necesita permiso de edición.",
    ),
    (
        DriveNotAFolderError, 422, "not_a_folder",
        "El elemento elegido no es una carpeta.",
    ),
    (
        DriveNotFoundError, 404, "folder_not_found",
        "La carpeta no existe o la cuenta conectada no puede verla.",
    ),
    (
        DriveQuotaError, 503, "quota_exceeded",
        "Se agotó la cuota de Google Drive. Inténtelo de nuevo más tarde.",
    ),
    (
        DriveSecureConnectionError, 502, "secure_connection_error",
        "No se pudo establecer una conexión segura con Google. Revise la red "
        "o el proxy. La verificación no se desactiva.",
    ),
    (
        DriveTransientError, 503, "network_error",
        "No se pudo comunicar con Google Drive. Inténtelo de nuevo.",
    ),
    (
        DriveAccessError, 403, "access_denied",
        "Google Drive denegó el acceso a la carpeta.",
    ),
    (
        DriveError, 502, "drive_error",
        "Google Drive respondió con un error.",
    ),
)


class FolderSelection(BaseModel):
    # Solo los IDs: nunca se acepta otra cosa, como un token del navegador.
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(max_length=256)
    target_id: str = Field(max_length=256)

    @field_validator("source_id", "target_id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        return queries.validate_file_id(value)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=status
    )


def _status_json(manager: ConnectionManager) -> dict:
    status = manager.status()
    account = status.account

    return {
        "state": status.state.value,
        "message": status.message,
        "error": status.error,
        "account": account and {
            "sub": account.sub,
            "email": account.email,
            "display_name": account.display_name,
        },
    }


def create_app(settings, manager: ConnectionManager) -> FastAPI:
    app = FastAPI(
        title="Drive Image Processor", docs_url=None, redoc_url=None, openapi_url=None
    )
    csrf_token = secrets.token_urlsafe(32)

    def _rejection(request: Request) -> JSONResponse | None:
        if request.headers.get("host") not in settings.allowed_hosts:
            return _error(400, "forbidden_host", "Host no permitido.")

        if not request.url.path.startswith("/api/"):
            return None

        origin = request.headers.get("origin")
        unsafe = request.method not in _SAFE_METHODS
        foreign = "forbidden_origin", "Origen no permitido."

        if origin is not None and origin not in settings.allowed_origins:
            return _error(403, *foreign)

        if request.headers.get("sec-fetch-site") not in _ALLOWED_FETCH_SITES:
            return _error(403, *foreign)

        if unsafe and origin is None:
            return _error(403, *foreign)

        if unsafe and not secrets.compare_digest(
            request.headers.get("x-csrf-token", "").encode("utf-8"),
            csrf_token.encode("utf-8"),
        ):
            return _error(403, "invalid_csrf_token", "Token CSRF inválido.")

        return None

    @app.middleware("http")
    async def protect(request: Request, call_next):
        response = _rejection(request)

        if response is None:
            try:
                response = await call_next(request)
            except Exception as exc:
                # Solo el tipo: el texto podría incluir datos de la solicitud.
                logger.error("Error inesperado (%s).", type(exc).__name__)
                response = _error(500, "unexpected_error", "Error inesperado.")

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # La clave de API de Picker está restringida por el origen que envía
        # el navegador: no se puede usar no-referrer.
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"

        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Sin el detalle de FastAPI, que repite lo recibido.
        return _error(422, "invalid_request", "La solicitud no es válida.")

    @app.get("/api/status")
    def status():
        return {
            "environment": settings.environment,
            "csrf_token": csrf_token,
            "connection": _status_json(manager),
            "picker": {
                "client_id": settings.picker_client_id,
                "api_key": settings.picker_api_key,
                "project_number": settings.project_number,
            },
        }

    @app.post("/api/connection/start")
    def start_connection():
        try:
            manager.start()
        except AlreadyConnectedError:
            return _error(
                409, "already_connected",
                "Ya hay una cuenta conectada o una autorización en curso.",
            )

        return _status_json(manager)

    @app.post("/api/connection/cancel")
    def cancel_connection():
        manager.cancel()
        return _status_json(manager)

    @app.post("/api/connection/disconnect")
    def disconnect():
        manager.disconnect()
        return _status_json(manager)

    @app.post("/api/folders/verify")
    def verify(selection: FolderSelection):
        try:
            read_service, write_service = manager.services()
            result = verify_folders(
                read_service, write_service, selection.source_id, selection.target_id
            )

        except NotConnectedError:
            return _error(401, "not_connected", "No hay una cuenta conectada.")

        except DriveAuthError:
            manager.mark_expired()
            return _error(
                401, "reconnect_required",
                "La autorización expiró o fue revocada. Vuelva a conectar la cuenta.",
            )

        except Exception as exc:
            for kind, status_code, code, message in _FOLDER_ERRORS:
                if isinstance(exc, kind):
                    return _error(status_code, code, message)
            raise

        return {
            "source": {"id": result.source.id, "name": result.source.name},
            "target": {"id": result.target.folder.id, "name": result.target.folder.name},
            "same_folder": result.same_folder,
        }

    if settings.frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=settings.frontend_dist, html=True))

    return app
