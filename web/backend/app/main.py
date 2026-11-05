"""
API local de la aplicación web.

Protección de las rutas: el host y el origen se validan en cada solicitud
(evita DNS rebinding y lecturas desde otros sitios), no se envían cabeceras
CORS, y toda solicitud que cambia algo necesita un Origin permitido y el
token CSRF de esta ejecución, que solo una página del mismo origen puede leer.
"""

import logging
import secrets

from fastapi import FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from image_processor.google_drive import queries
from image_processor.processing import (
    BACKGROUNDS,
    DEFAULT_BACKGROUND,
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    MAX_PIXELS,
    MAX_SIDE,
    MIN_SIDE,
    OutputOptions,
)
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from .connection import AlreadyConnectedError, ConnectionManager, NotConnectedError
from .errors import RECONNECT_REQUIRED, describe_error
from .folders import verify_folders
from .images import SampleSource, thumbnail
from .images import ImageNotAvailable as SampleNotAvailable
from .jobs import ImageNotAvailable, JobInProgressError, JobManager, PreviewNotUsableError


logger = logging.getLogger(__name__)

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Sec-Fetch-Site aceptados. Otro puerto de localhost es "same-site".
_ALLOWED_FETCH_SITES = {None, "same-origin", "none"}

class FolderSelection(BaseModel):
    # Solo los IDs: nunca se acepta otra cosa, como un token del navegador.
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(max_length=256)
    target_id: str = Field(max_length=256)

    @field_validator("source_id", "target_id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        return queries.validate_file_id(value)


class OutputSettings(BaseModel):
    """Tamaño y fondo del resultado. Los límites los valida el motor."""

    model_config = ConfigDict(extra="forbid")

    width: StrictInt
    height: StrictInt
    background: str = Field(max_length=32)


class PreviewRequest(FolderSelection):
    options: OutputSettings | None = None


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


_NOT_CONNECTED = 401, "not_connected", "No hay una cuenta conectada."
_NO_IMAGE = 404, "image_not_available", "Esa imagen no está en este equipo."


def _public(job: dict | None) -> dict | None:
    """El trabajo sin los nombres de los archivos locales."""

    if job is None:
        return None
    return {key: value for key, value in job.items() if key != "files"}
_JOB_IN_PROGRESS = (
    409, "job_in_progress", "Hay un trabajo en curso. Espere a que termine."
)


def create_app(
    settings,
    manager: ConnectionManager,
    jobs: JobManager | None = None,
    samples: SampleSource | None = None,
) -> FastAPI:
    jobs = jobs or JobManager(settings, manager)
    samples = samples or SampleSource()
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
            "output": {
                "width": DEFAULT_WIDTH,
                "height": DEFAULT_HEIGHT,
                "background": DEFAULT_BACKGROUND,
                "backgrounds": list(BACKGROUNDS),
                "min_side": MIN_SIDE,
                "max_side": MAX_SIDE,
                "max_pixels": MAX_PIXELS,
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
        try:
            jobs.while_idle(manager.disconnect)
        except JobInProgressError:
            return _error(*_JOB_IN_PROGRESS)

        return _status_json(manager)

    @app.post("/api/folders/verify")
    def verify(selection: FolderSelection):
        try:
            read_service, write_service = manager.services()
            result = verify_folders(
                read_service, write_service, selection.source_id, selection.target_id
            )

        except NotConnectedError:
            return _error(*_NOT_CONNECTED)

        except Exception as exc:
            info = describe_error(exc)

            if info is None:
                raise

            if info.code == RECONNECT_REQUIRED:
                manager.mark_expired()

            return _error(info.status, info.code, info.message)

        return {
            "source": {"id": result.source.id, "name": result.source.name},
            "target": {"id": result.target.folder.id, "name": result.target.folder.name},
            "same_folder": result.same_folder,
        }

    @app.get("/api/jobs/current")
    def current_job():
        account = manager.status().account
        return {"job": _public(jobs.current(account.sub if account else None))}

    def _connected_sub() -> str | None:
        status_ = manager.status()
        if status_.state.value != "connected" or status_.account is None:
            return None
        return status_.account.sub

    @app.get("/api/jobs/current/images/{file_id}/{kind}")
    def job_image(
        file_id: str, kind: str = Path(pattern="^(original|result)$"), aligned: bool = False
    ):
        sub = _connected_sub()

        if sub is None:
            return _error(*_NOT_CONNECTED)

        try:
            queries.validate_file_id(file_id)
            path = jobs.local_image(sub, file_id, kind)
        except (ValueError, ImageNotAvailable):
            return _error(*_NO_IMAGE)

        if aligned and kind == "original":
            try:
                result = jobs.local_image(sub, file_id, "result")
                content = samples.aligned(path, result, jobs.account_dir(sub) / "compare")
                return Response(content, media_type="image/jpeg")
            except Exception as exc:
                # Sin alinear, el comparador sigue funcionando.
                logger.info("Original sin alinear (%s).", type(exc).__name__)

        try:
            content, media_type = thumbnail(path, lossless=kind == "result")
        except SampleNotAvailable:
            return _error(*_NO_IMAGE)

        return Response(content, media_type=media_type)

    @app.get("/api/sample")
    def sample(width: int, height: int, background: str = Query(max_length=32)):
        sub = _connected_sub()

        if sub is None:
            return _error(*_NOT_CONNECTED)

        try:
            options = OutputOptions(width=width, height=height, background=background)
        except ValueError as exc:
            return _error(422, "invalid_options", str(exc))

        try:
            content = samples.render(samples.cutout(jobs.account_dir(sub)), options)
        except SampleNotAvailable:
            return _error(
                404, "no_sample",
                "Todavía no hay fotos procesadas o descargadas para mostrar un ejemplo.",
            )
        except Exception as exc:
            info = describe_error(exc)
            if info is None:
                raise
            return _error(info.status, info.code, info.message)

        return Response(content, media_type="image/png")

    @app.post("/api/jobs/preview")
    def start_preview(request: PreviewRequest):
        try:
            options = (
                OutputOptions(**request.options.model_dump())
                if request.options
                else OutputOptions()
            )
        except ValueError as exc:
            # Mensajes del motor: indican el límite, sin datos sensibles.
            return _error(422, "invalid_options", str(exc))

        try:
            return _public(jobs.start_preview(request.source_id, request.target_id, options))
        except NotConnectedError:
            return _error(*_NOT_CONNECTED)
        except JobInProgressError:
            return _error(*_JOB_IN_PROGRESS)

    @app.post("/api/jobs/{preview_id}/run")
    def start_run(preview_id: str = Path(pattern="^[0-9a-f]{32}$")):
        # Las carpetas salen de la vista previa, nunca de esta solicitud.
        try:
            return _public(jobs.start_run(preview_id))
        except NotConnectedError:
            return _error(*_NOT_CONNECTED)
        except JobInProgressError:
            return _error(*_JOB_IN_PROGRESS)
        except PreviewNotUsableError as exc:
            return _error(409, exc.code, exc.message)

    if settings.frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=settings.frontend_dist, html=True))

    return app
