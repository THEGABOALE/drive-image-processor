"""
Vista previa y ejecución del motor, en segundo plano.

Se llama a sync.sync directamente, sin subprocesos. El motor no admite
ejecuciones concurrentes y el modelo de IA usa mucha CPU: hay un solo trabajo
a la vez, en su propio hilo y con sus propios servicios de Drive.

Una ejecución solo parte de la vista previa vigente, terminada y sin usar, y
toma de ella las carpetas: nunca las recibe del frontend. El motor reconstruye
el plan al ejecutar y no admite una lista fija de fotos, así que lo agregado a
la carpeta después de la vista previa también se procesa; por eso la vista
previa vence.

Los datos locales que el motor necesita (originales, resultados y registros)
están en <datos>/accounts/<cuenta>/folders/<origen>. Son por cuenta porque
drive.file depende de ella, y por origen porque el motor exige una carpeta de
resultados por cada carpeta de Drive. Los nombres son hashes cortos para no
acercarse al límite de longitud de las rutas de Windows.

El último trabajo de cada cuenta se guarda en disco. Uno que figura en curso
sin estar activo se interrumpió porque se cerró la aplicación: volver a
ejecutar retoma con los registros del motor, sin duplicar.

PNG borrados de Drive: si un PNG que la aplicación subió ya no existe en Drive
(se borró definitivamente), el motor no lo vuelve a subir solo, por si se
borró a propósito. La pantalla lo muestra como una nota ("removed") y no como
un error. Para volver a generarlo, se confirma con Drive que ya no existe, se
aparta el registro local de esa subida (se renombra, no se borra) y se crea una
vista previa nueva: nada se sube hasta que se confirme.
"""

import copy
import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from image_processor import batch
from image_processor.google_drive import client as drive_client
from image_processor.google_drive import downloads, queries, sync, uploads
from image_processor.google_drive.errors import DriveNotFoundError
from image_processor.processing import OutputOptions

from .connection import NotConnectedError, State
from .errors import RECONNECT_REQUIRED, ErrorInfo, describe_error
from .folders import verify_folders
from .token_store import write_atomic


logger = logging.getLogger(__name__)

PREVIEW = "preview"
RUN = "run"

RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"
INTERRUPTED = "interrupted"

# Estado propio de la pantalla: un PNG subido antes que ya no está en Drive.
REMOVED = "removed"

# Comienzo del motivo con el que el motor informa ese caso (uploads.py).
_REMOVED_REASON = "el PNG subido antes ya no existe en Drive"

PREVIEW_MAX_AGE_SECONDS = 3600

_LAST_JOB_NAME = "last_job.json"

_UNEXPECTED = ErrorInfo(500, "unexpected_error", "Ocurrió un error inesperado.")

_INTERRUPTED_ERROR = {
    "code": "interrupted",
    "message": (
        "El trabajo se interrumpió porque se cerró la aplicación. Genere una "
        "nueva vista previa: lo ya hecho se retoma sin duplicar."
    ),
}

_FAILED_STATUSES = {
    sync.DOWNLOAD: {downloads.FAILED, downloads.REJECTED},
    sync.PROCESSING: {batch.FAILED, batch.SEGMENTATION_FAILED},
    sync.UPLOAD: {uploads.FAILED, uploads.REJECTED, uploads.UNCERTAIN},
}


class JobInProgressError(Exception):
    """Ya hay un trabajo en curso."""


class ImageNotAvailable(Exception):
    """El último trabajo no tiene esa imagen en este equipo."""


class PreviewNotUsableError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _account_dir(settings, sub: str) -> Path:
    return Path(settings.data_dir) / "accounts" / _short_hash(sub)


def folder_dirs(settings, sub: str, source_id: str) -> tuple[Path, Path]:
    """Las carpetas locales del motor: descargas y resultados."""

    root = _account_dir(settings, sub) / "folders" / _short_hash(source_id)
    return root / "downloads", root / "outputs"


def _local_files(summary) -> dict:
    """Los nombres locales que el motor usó para cada original del plan."""

    return {
        item.file_id: {
            "original": item.revision.local_name if item.revision else None,
            "result": item.result.output_name if item.result else None,
        }
        for item in summary.plan
    }


def _is_removed(item) -> bool:
    return (
        item.status == sync.FAILED
        and item.stage == sync.UPLOAD
        and (item.reason or "").startswith(_REMOVED_REASON)
    )


def _forgettable_record(path: Path, file_id: str, target_id: str) -> dict | None:
    """El registro de subida de esa foto en ese destino, o None."""

    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None

    if not (
        isinstance(record, dict)
        and record.get("source_file_id") == file_id
        and record.get("target_folder_id") == target_id
        and record.get("status") in (uploads.LEDGER_COMPLETED, uploads.LEDGER_MISMATCH)
        and isinstance(record.get("remote_id"), str)
    ):
        return None

    try:
        queries.validate_file_id(record["remote_id"])
    except ValueError:
        return None

    return record


def _gone_from_drive(write_service, remote_id: str) -> bool:
    """Solo lectura: el PNG ya no existe (en la papelera todavía existe)."""

    try:
        drive_client.execute(
            write_service.files().get(fileId=remote_id, fields="id", supportsAllDrives=True)
        )
    except DriveNotFoundError:
        return True

    return False


def _can_run(kind: str, stopped, counts: dict) -> bool:
    """Una vista previa sin detenciones y con algo por hacer o por confirmar."""

    return (
        kind == PREVIEW
        and stopped is None
        and counts[sync.PLANNED] + counts[sync.PENDING] > 0
    )


def _start_daemon_thread(target) -> None:
    threading.Thread(target=target, daemon=True, name="drive-job").start()


def _new_progress(total: int | None) -> dict:
    return {
        "total": total,
        "stage": None,
        "current": None,
        sync.DOWNLOAD: {"done": 0, "failed": 0},
        sync.PROCESSING: {"done": 0, "failed": 0},
        sync.UPLOAD: {"done": 0, "failed": 0},
    }


class JobManager:
    def __init__(
        self,
        settings,
        connection,
        *,
        runner=sync.sync,
        start_thread=_start_daemon_thread,
        now=time.time,
    ):
        self._settings = settings
        self._connection = connection
        self._runner = runner
        self._start_thread = start_thread
        self._now = now
        self._lock = threading.RLock()
        self._job = None
        self._sub = None
        self._active = False

    # Consulta -----------------------------------------------------------------

    def current(self, sub: str | None) -> dict | None:
        """El último trabajo de la cuenta. Es una copia: no altera el estado."""

        if not sub:
            return None

        with self._lock:
            if self._sub != sub and self._active:
                return None

            self._load(sub)
            return copy.deepcopy(self._job)

    def while_idle(self, action) -> None:
        """Ejecuta action solo si no hay un trabajo en curso."""

        with self._lock:
            if self._active:
                raise JobInProgressError()
            action()

    # Inicio -------------------------------------------------------------------

    def start_preview(
        self, source_id: str, target_id: str, options: OutputOptions = OutputOptions()
    ) -> dict:
        return self._start(PREVIEW, source_id, target_id, None, options)

    def start_run(self, preview_id: str) -> dict:
        with self._lock:
            if self._active:
                raise JobInProgressError()

            sub = self._connected_sub()
            self._load(sub)
            preview = self._usable_preview(preview_id)

            # Las opciones son las de la vista previa (o las del motor, si se
            # guardó antes de que existieran).
            return self._start(
                RUN,
                preview["source"]["id"],
                preview["target"]["id"],
                len(preview["summary"]["items"]),
                OutputOptions(**(preview.get("options") or {})),
            )

    def _start(
        self,
        kind: str,
        source_id: str,
        target_id: str,
        total: int | None,
        options: OutputOptions,
    ) -> dict:
        with self._lock:
            if self._active:
                raise JobInProgressError()

            sub = self._connected_sub()
            job = {
                "id": uuid.uuid4().hex,
                "kind": kind,
                "status": RUNNING,
                "source": {"id": source_id, "name": None},
                "target": {"id": target_id, "name": None},
                "same_folder": source_id == target_id,
                "options": asdict(options),
                "created_at": self._now(),
                "finished_at": None,
                "progress": _new_progress(total),
                "summary": None,
                "error": None,
            }
            self._job = job
            self._sub = sub
            self._active = True
            self._save(sub, job)

        self._start_thread(lambda: self._execute(job, sub))

        with self._lock:
            return copy.deepcopy(job)

    def _connected_sub(self) -> str:
        status = self._connection.status()

        if status.state is not State.CONNECTED or status.account is None:
            raise NotConnectedError()

        return status.account.sub

    def _usable_preview(self, preview_id: str) -> dict:
        job = self._job

        if job is None or job["id"] != preview_id or job["kind"] != PREVIEW:
            raise PreviewNotUsableError(
                "preview_not_current",
                "Esta vista previa ya no es la vigente. Genere una nueva.",
            )

        if job["status"] != FINISHED:
            raise PreviewNotUsableError(
                "preview_not_finished",
                "La vista previa no terminó bien. Genere una nueva.",
            )

        if not job["summary"]["can_run"]:
            raise PreviewNotUsableError(
                "nothing_to_run",
                "La vista previa no tiene nada que procesar.",
            )

        if self._now() - job["finished_at"] > PREVIEW_MAX_AGE_SECONDS:
            raise PreviewNotUsableError(
                "preview_expired",
                "La vista previa venció: la carpeta pudo cambiar. Genere una nueva.",
            )

        return job

    # PNG borrados de Drive ----------------------------------------------------

    def regenerate_removed(self, job_id: str) -> dict:
        """
        Aparta el registro de cada PNG del trabajo que ya no existe en Drive y
        crea una vista previa nueva con las mismas carpetas y opciones.
        """

        with self._lock:
            if self._active:
                raise JobInProgressError()

            sub = self._connected_sub()
            self._load(sub)
            job = self._job

            if job is None or job["id"] != job_id or job["status"] != FINISHED:
                raise PreviewNotUsableError(
                    "job_not_current",
                    "Ese trabajo ya no es el vigente. Genere una vista previa nueva.",
                )

            removed = [
                item["file_id"] for item in job["summary"]["items"]
                if item["status"] == REMOVED
            ]

            if not removed:
                raise PreviewNotUsableError(
                    "nothing_to_regenerate",
                    "Este trabajo no tiene PNG borrados de Drive para volver a generar.",
                )

            source_id, target_id = job["source"]["id"], job["target"]["id"]
            options = OutputOptions(**(job.get("options") or {}))
            files = job.get("files") or {}

        _, write_service = self._connection.services()
        _, output_dir = folder_dirs(self._settings, sub, source_id)
        forgotten = 0

        for file_id in removed:
            output_name = (files.get(file_id) or {}).get("result")

            if not output_name:
                continue

            path = uploads.ledger_path(output_dir, target_id, output_name)
            record = _forgettable_record(path, file_id, target_id)

            if record is None or not _gone_from_drive(write_service, record["remote_id"]):
                continue

            # Apartado y no borrado: el motor solo lee los .json.
            path.replace(path.with_name(f"{path.name}.borrado-{int(self._now())}"))
            forgotten += 1

        if not forgotten:
            raise PreviewNotUsableError(
                "removed_not_confirmed",
                "No se pudo confirmar que esos PNG ya no estén en Drive: no se vuelven "
                "a generar. Si están en la papelera, restáurelos desde allí.",
            )

        return self._start(PREVIEW, source_id, target_id, None, options)

    # Ejecución ----------------------------------------------------------------

    def _execute(self, job: dict, sub: str) -> None:
        source_id = job["source"]["id"]
        target_id = job["target"]["id"]

        try:
            # Se construyen en este hilo: no son seguros entre hilos.
            read_service, write_service = self._connection.services()
            selection = verify_folders(read_service, write_service, source_id, target_id)

            with self._lock:
                job["source"]["name"] = selection.source.name
                job["target"]["name"] = selection.target.folder.name

            downloads_dir, output_dir = folder_dirs(self._settings, sub, source_id)
            summary = self._runner(
                read_service,
                write_service,
                folder_id=source_id,
                downloads_dir=downloads_dir,
                output_dir=output_dir,
                target_folder_id=target_id,
                limit=None,
                options=OutputOptions(**job["options"]),
                dry_run=job["kind"] == PREVIEW,
                on_event=lambda stage, *details: self._on_event(job, stage, details),
            )
            result = self._summary_json(job["kind"], summary)
            files = _local_files(summary)

        except Exception as exc:
            self._finish(sub, job, FAILED, error=self._error_json(exc))

        else:
            self._finish(sub, job, FINISHED, summary=result, files=files)

    def _on_event(self, job: dict, stage: str, details: tuple) -> None:
        if stage == sync.DOWNLOAD:
            (item,) = details
            # Lo que nunca llegó a ser una imagen del plan no cuenta.
            if item.file_id is None:
                return
            name = item.name
        elif stage == sync.PROCESSING:
            source, item = details
            name = source.remote_name
        else:
            (item,) = details
            name = item.output_name

        with self._lock:
            progress = job["progress"]
            progress["stage"] = stage
            progress["current"] = name
            progress[stage]["done"] += 1
            if item.status in _FAILED_STATUSES[stage]:
                progress[stage]["failed"] += 1

    def _summary_json(self, kind: str, summary) -> dict:
        stopped = None

        if summary.stopped is not None:
            stopped = {
                "stage": summary.stopped_stage,
                **self._error_json(summary.stopped),
            }

        counts = {
            status: summary.count(status)
            for status in (
                sync.COMPLETE, sync.PLANNED, sync.FAILED, sync.PENDING, sync.NOT_STARTED,
            )
        }
        removed = {item.file_id for item in summary.plan if _is_removed(item)}
        counts[sync.FAILED] -= len(removed)
        counts[REMOVED] = len(removed)

        return {
            "counts": counts,
            "uploaded_now": summary.upload.count(uploads.UPLOADED) if summary.upload else 0,
            # Un PNG borrado a propósito no deja el trabajo incompleto.
            "complete": (
                stopped is None and counts[sync.FAILED] == 0 and counts[sync.PENDING] == 0
            ),
            "can_run": _can_run(kind, stopped, counts),
            "stopped": stopped,
            "warnings": list(summary.warnings),
            "items": [
                {
                    "file_id": item.file_id,
                    "name": item.original.name,
                    "status": REMOVED if item.file_id in removed else item.status,
                    "stage": item.stage,
                    "reason": None if item.file_id in removed else item.reason,
                    "actions": list(item.actions),
                    "undetermined": list(item.undetermined),
                    "notes": list(item.notes),
                    "related": [
                        {"kind": related.kind, "name": related.name}
                        for related in item.related
                    ],
                    # Si hay copias locales para el comparador de la pantalla.
                    "images": {
                        "original": item.revision is not None,
                        "result": item.result is not None,
                    },
                }
                for item in summary.plan
            ],
        }

    def _error_json(self, exc: BaseException) -> dict:
        info = describe_error(exc)

        if info is None:
            # Solo el tipo: el texto podría incluir datos de la solicitud.
            logger.error("Error inesperado en un trabajo (%s).", type(exc).__name__)
            info = _UNEXPECTED

        if info.code == RECONNECT_REQUIRED:
            self._connection.mark_expired()

        return {"code": info.code, "message": info.message}

    def _finish(
        self, sub: str, job: dict, status: str, *, summary=None, error=None, files=None
    ) -> None:
        with self._lock:
            job["status"] = status
            job["summary"] = summary
            job["error"] = error
            job["files"] = files or {}
            job["finished_at"] = self._now()
            job["progress"]["current"] = None
            self._active = False
            self._save(sub, job)

    # Imágenes -----------------------------------------------------------------

    def local_image(self, sub: str | None, file_id: str, kind: str) -> Path:
        """
        La copia local de un original o de un resultado del último trabajo.
        Solo nombres que el motor informó para ese trabajo, dentro de sus
        carpetas.
        """
        if not sub:
            raise NotConnectedError()

        with self._lock:
            if self._sub != sub and self._active:
                raise ImageNotAvailable()
            self._load(sub)
            job = self._job
            name = ((job or {}).get("files") or {}).get(file_id, {}).get(kind)
            source_id = job and job["source"]["id"]

        if not name or name != Path(name).name or name.startswith(".") or "\\" in name:
            raise ImageNotAvailable()

        downloads_dir, output_dir = folder_dirs(self._settings, sub, source_id)
        base = downloads_dir if kind == "original" else output_dir
        path = base / name

        if path.resolve().parent != base.resolve() or not path.is_file():
            raise ImageNotAvailable()

        return path

    def account_dir(self, sub: str) -> Path:
        return _account_dir(self._settings, sub)

    # Persistencia -------------------------------------------------------------

    def _path(self, sub: str) -> Path:
        return _account_dir(self._settings, sub) / _LAST_JOB_NAME

    def _save(self, sub: str, job: dict) -> None:
        try:
            write_atomic(self._path(sub), json.dumps(job).encode("utf-8"))
        except OSError as exc:
            # No conservar el resumen no cambia lo que hizo el motor.
            logger.error("No se pudo guardar el trabajo (%s).", type(exc).__name__)

    def _load(self, sub: str) -> None:
        if self._sub == sub:
            return

        self._sub = sub
        self._job = None

        try:
            job = json.loads(self._path(sub).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return

        if not isinstance(job, dict):
            return

        if job.get("status") == RUNNING:
            job["status"] = INTERRUPTED
            job["error"] = dict(_INTERRUPTED_ERROR)
            job["finished_at"] = self._now()
            self._save(sub, job)

        self._job = job
