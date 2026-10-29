"""
Sincronización bajo demanda de una carpeta de Drive (GD-04).

Coordina las etapas existentes sin reimplementarlas: listado (GD-01),
descarga (GD-02), procesamiento (GD-03A) y subida (GD-03B).

Plan
----

Se lista la carpeta una sola vez y se seleccionan los originales en el
orden de las descargas; el límite se aplica una sola vez. Por cada
original, el plan registra en memoria:

- original: fileId, nombre, MD5 y tamaño del listado;
- revisión: la copia local que la descarga verificó en esta ejecución
  contra ese contenido (su registro de procedencia);
- resultado: la entrada del manifiesto (nombre e identidad, ver
  batch.result_identity) del procesamiento pedido para esa copia;
- subida: el PNG confirmado en la carpeta de destino.

Cada etapa recibe solo lo que la anterior dejó para ese mismo original:
ninguna elige revisiones por versión ni reemplaza una foto fallida por
otra que quedó fuera de la selección. No hay un registro propio: al
repetir la sincronización el plan se reconstruye, y cada etapa verifica
o recupera con sus registros (procedencia, manifiesto y subidas).

Contenido y metadatos
---------------------

La identidad del contenido es el fileId, el tamaño y el MD5 de Drive
(y su SHA-256 si lo informa), más el SHA-256 de los bytes locales. El
nombre, la fecha, la versión y headRevisionId son metadatos:

- un renombre reutiliza la copia local (GD-02) y no reprocesa ni sube;
  el PNG remoto conserva su nombre (nunca se renombra);
- si el contenido cambió entre el listado y la descarga, la imagen no
  se descarga ni avanza: queda fallida y se informa;
- un cambio durante la descarga lo detecta la descarga (GD-02).

PNG generados
-------------

Los resultados nunca se procesan como originales, aunque el destino sea
la misma carpeta: el listado excluye los TRANSFORMADA_ y, además, los
archivos que la aplicación subió según su registro de subidas (por su
ID remoto, aunque se hayan renombrado, o por su MD5, si se copiaron).

Errores
-------

Se valida antes de empezar: el manifiesto y las carpetas locales, las
carpetas de Drive y el permiso de escritura de drive.file en el destino
(sin acceso, nada se descarga ni se procesa). Una etapa que se detiene
(autorización, permisos, cuota, conexión segura, red, modelo,
manifiesto o disco local lleno) impide empezar las siguientes. Las
imágenes que fallan por sí solas no avanzan y las demás siguen.

Los archivos y resultados que no pertenecen al plan (archivos sueltos,
originales que ya no están en Drive) solo se informan como advertencias.

Vista previa (dry_run): ni archivos ni carpetas locales, ni manifiesto,
ni registros, ni limpieza de temporales, ni modelo, ni IDs o archivos
en Drive. Lo que depende de una etapa posterior se informa como no
determinable.

No se admiten ejecuciones concurrentes sobre las mismas carpetas.
"""

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .. import batch
from ..background_removal import ModelInitializationError
from ..manifest import Manifest, ManifestError, ResultIdentity
from ..processing import OutputOptions
from . import client, downloads, local_processing, queries, uploads
from .auth import AUTH_WRITE_COMMAND
from .errors import (
    DriveAccessError,
    DriveAuthError,
    DriveError,
    DriveSecureConnectionError,
)


DEFAULT_MODEL = local_processing.DEFAULT_MODEL

# Etapas.
DOWNLOAD = "download"
PROCESSING = "processing"
UPLOAD = "upload"

# Estado de cada original del plan.
COMPLETE = "complete"          # subido o verificado en Drive
PLANNED = "planned"            # vista previa: hay acciones por hacer
FAILED = "failed"              # una etapa falló: no avanzó
PENDING = "pending"            # recuperable en la próxima ejecución
NOT_STARTED = "not_started"    # una etapa anterior detuvo todo

_STOPPING = (
    DriveError,
    downloads.LocalDiskFullError,
    ModelInitializationError,
    ManifestError,
    local_processing.ForeignFolderError,
)


@dataclass(frozen=True)
class Revision:
    """Copia local verificada contra el contenido del original."""

    local_name: str
    sha256: str
    md5_checksum: str
    size: int
    remote_name: str


@dataclass(frozen=True)
class Authorized:
    """Entrada exacta del manifiesto que la subida puede usar."""

    output_name: str
    identity: ResultIdentity


@dataclass
class PlanItem:
    original: queries.DriveImage
    revision: Revision | None = None
    result: Authorized | None = None
    download: downloads.DownloadItem | None = None
    processing: str | None = None
    upload: uploads.UploadItem | None = None
    status: str = NOT_STARTED
    stage: str | None = None
    reason: str | None = None
    # Vista previa: acciones conocidas y etapas que no se pueden prever.
    actions: list[str] = field(default_factory=list)
    undetermined: list[str] = field(default_factory=list)
    related: list[uploads.RemoteResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def file_id(self) -> str:
        return self.original.id

    def fail(self, stage, reason, status=FAILED):
        self.status, self.stage, self.reason = status, stage, reason


@dataclass
class SyncSummary:
    folder: queries.DriveFolder
    target: uploads.TargetAccess
    dry_run: bool
    plan: list[PlanItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stopped: BaseException | None = None
    stopped_stage: str | None = None
    download: downloads.DownloadSummary | None = None
    processing: local_processing.LocalProcessingResult | None = None
    upload: uploads.UploadSummary | None = None

    def count(self, status: str) -> int:
        return sum(item.status == status for item in self.plan)

    @property
    def complete(self) -> bool:
        """Sin detenciones ni imágenes fallidas o pendientes."""

        return self.stopped is None and not any(
            item.status in (FAILED, PENDING) for item in self.plan
        )


def _stop(summary, stage, exc, items):
    summary.stopped = exc
    summary.stopped_stage = stage

    for item in items:
        if item.status == NOT_STARTED:
            # La primera etapa que le faltó a cada imagen.
            if item.result is not None:
                item.stage = UPLOAD
            elif item.revision is not None:
                item.stage = PROCESSING
            else:
                item.stage = stage


def _verified_revision(downloads_dir: Path, item: PlanItem) -> Revision | None:
    """
    La copia que la descarga verificó, si corresponde exactamente al
    contenido seleccionado (fileId, MD5 y tamaño del listado).
    """
    download = item.download

    try:
        record = downloads.read_record(downloads_dir, download.local_name)
    except downloads.InvalidRecordError:
        record = None

    original = item.original

    if (
        record is None
        or record["file_id"] != original.id
        or record["status"] != downloads.COMPLETED
        or (original.md5_checksum is not None and record["md5_checksum"] != original.md5_checksum)
        or (original.size is not None and record["size"] != original.size)
    ):
        return None

    return Revision(
        local_name=download.local_name,
        sha256=record["sha256"],
        md5_checksum=record["md5_checksum"],
        size=record["size"],
        remote_name=record["name"],
    )


def _download_stage(ctx, summary):
    plan = summary.plan
    by_id = {item.file_id: item for item in plan}

    try:
        result = downloads.download_folder(
            ctx.read, summary.folder.id, ctx.downloads_dir,
            listing=ctx.listing,
            file_ids=[item.file_id for item in plan],
            dry_run=ctx.dry_run,
            stop_on_disk_full=True,
            # Además de autorización, cuota y red: un acceso denegado o una
            # conexión segura fallida detienen la sincronización.
            stop_on_errors=(DriveAccessError, DriveSecureConnectionError),
            max_file_bytes=ctx.max_file_bytes,
            sleep=ctx.sleep,
            execute=ctx.execute,
            on_item=ctx.event(DOWNLOAD),
        )
    except _STOPPING as exc:
        _stop(summary, DOWNLOAD, exc, plan)
        return False
    except OSError as exc:
        # Por ejemplo, al crear la carpeta de descargas.
        if not downloads.is_disk_full(exc):
            raise
        _stop(summary, DOWNLOAD, downloads.LocalDiskFullError(
            None, str(ctx.downloads_dir), downloads.PHASE_LOCAL
        ), plan)
        return False

    summary.download = result

    for download in result.items:
        item = by_id.get(download.file_id)

        if item is None or item.download is not None:
            continue

        item.download = download
        status = download.status

        if status == downloads.PLANNED:
            item.status = PLANNED
            item.stage = DOWNLOAD
            item.actions.append(DOWNLOAD)
            item.undetermined += [PROCESSING, UPLOAD]
            continue

        if status in (downloads.REJECTED, downloads.FAILED):
            item.fail(DOWNLOAD, download.reason)
            continue

        if status == downloads.DOWNLOADED and not download.registered:
            item.fail(DOWNLOAD, download.reason, PENDING)
            continue

        if status == downloads.RECOVERED and ctx.dry_run:
            # Su registro sigue pendiente hasta la ejecución real: el
            # procesamiento no puede evaluarlo todavía.
            item.status = PLANNED
            item.stage = DOWNLOAD
            item.actions.append(DOWNLOAD)
            item.undetermined += [PROCESSING, UPLOAD]
            item.notes.append(download.reason)
            continue

        revision = _verified_revision(ctx.downloads_dir, item)

        if revision is None:
            item.fail(
                DOWNLOAD,
                "la copia descargada no corresponde al contenido seleccionado; "
                "no se procesa ni se sube.",
            )
            continue

        item.revision = revision

        if download.reason and status in (downloads.VERIFIED, downloads.RECOVERED):
            item.notes.append(download.reason)

    if result.aborted is not None:
        _stop(summary, DOWNLOAD, result.aborted, plan)
        return False

    return True


def _processing_stage(ctx, summary):
    ready = [item for item in summary.plan if item.revision is not None and item.status == NOT_STARTED]

    if not ready:
        return True

    by_id = {item.file_id: item for item in ready}

    try:
        result = local_processing.process_downloads(
            ctx.downloads_dir, ctx.output_dir, summary.folder.id,
            model_name=ctx.model_name,
            options=ctx.options,
            dry_run=ctx.dry_run,
            selection=[(item.file_id, item.revision.local_name) for item in ready],
            on_item=ctx.event(PROCESSING),
        )
    except _STOPPING as exc:
        _stop(summary, PROCESSING, exc, ready)
        return False

    summary.processing = result

    for excluded in result.rejected:
        item = by_id.get(excluded.file_id)
        if item is not None and item.status == NOT_STARTED:
            item.fail(PROCESSING, excluded.reason)

    prepared = {entry.file_id: entry for entry in result.prepared}
    sources = {str(source.path): source for source in result.selected}

    for processed in result.batch.items:
        source = sources.get(str(processed.source))
        item = by_id.get(source.file_id) if source is not None else None

        if item is None or item.status != NOT_STARTED:
            continue

        item.processing = processed.status

        if processed.status == batch.PLANNED:
            item.status = PLANNED
            item.stage = PROCESSING
            item.actions.append(PROCESSING)
            item.undetermined.append(UPLOAD)
            continue

        if processed.status not in (batch.PROCESSED, batch.VERIFIED, batch.RECOVERED):
            item.fail(PROCESSING, processed.message or "no se pudo procesar.")
            continue

        entry = prepared.get(item.file_id)

        if entry is None or not entry.ready:
            item.fail(PROCESSING, entry.reason if entry else "no tiene resultado.")
            continue

        item.result = Authorized(
            entry.output_name,
            batch.result_identity(source.record, ctx.model_name, ctx.options),
        )

    for item in ready:
        if item.status == NOT_STARTED and item.result is None:
            item.fail(PROCESSING, "no se obtuvo un resultado verificado.")

    # Advertencias: lo que no pertenece al plan no cambia el resultado.
    # Las copias seleccionadas con problemas ya figuran como fallidas.
    listed = {image.id for image in ctx.listing.images}
    selected_names = {item.revision.local_name for item in ready}
    others = [
        excluded for excluded in result.discovery.rejected
        if excluded.name not in selected_names
    ]
    copies = [excluded for excluded in others if excluded.file_id in by_id]
    loose = [excluded for excluded in others if excluded.file_id not in by_id]
    gone = sorted({
        source.file_id for source in result.discovery.valid if source.file_id not in listed
    })

    if copies:
        summary.warnings.append(
            f"{len(copies)} otra(s) copia(s) local(es) de originales del plan "
            "no se usaron (revisiones anteriores alteradas o copias "
            "duplicadas); se procesa solo la copia verificada en esta "
            "ejecución y las demás no se modifican."
        )

    if loose:
        summary.warnings.append(
            f"{len(loose)} archivo(s) de la carpeta de descargas no tienen "
            "procedencia válida y no forman parte del plan; no se procesan."
        )

    if gone:
        summary.warnings.append(
            f"{len(gone)} original(es) descargado(s) antes ya no están en la "
            "carpeta de Drive; sus copias y resultados no se modifican."
        )

    return True


def _upload_stage(ctx, summary):
    ready = [item for item in summary.plan if item.result is not None and item.status == NOT_STARTED]

    if not ready:
        return True

    by_id = {item.file_id: item for item in ready}

    try:
        result = uploads.upload_results(
            ctx.read, ctx.write, ctx.output_dir, summary.folder.id,
            target_folder_id=summary.target.folder.id,
            model_name=ctx.model_name,
            options=ctx.options,
            dry_run=ctx.dry_run,
            authorized=[
                (item.file_id, item.result.output_name, item.result.identity)
                for item in ready
            ],
            sleep=ctx.sleep,
            execute=ctx.execute,
            on_item=ctx.event(UPLOAD),
        )
    except _STOPPING as exc:
        _stop(summary, UPLOAD, exc, ready)
        return False

    summary.upload = result

    for uploaded in result.items:
        item = by_id.get(uploaded.file_id)

        if item is None or item.upload is not None:
            continue

        item.upload = uploaded

        if uploaded.status in (uploads.UPLOADED, uploads.VERIFIED, uploads.RECOVERED):
            item.status = COMPLETE
            if uploaded.status == uploads.RECOVERED:
                item.notes.append(uploaded.reason)
        elif uploaded.status == uploads.PLANNED:
            item.status = PLANNED
            item.stage = UPLOAD
            item.actions.append(UPLOAD)
        elif uploaded.status == uploads.UNCERTAIN:
            item.fail(UPLOAD, uploaded.reason, PENDING)
        else:
            item.fail(UPLOAD, uploaded.reason)

    if result.aborted is not None:
        _stop(summary, UPLOAD, result.aborted, ready)
        return False

    return True


def _report_related(ctx, summary):
    """PNG remotos anteriores u de otras configuraciones (solo lectura)."""

    items = [item for item in summary.plan if item.result is not None]

    if not items:
        return

    try:
        grouped = uploads.remote_results_by_source(
            summary.target.own, summary.target.folder.id, execute=ctx.execute
        )
        manifest = Manifest.load(ctx.output_dir, clean=False)
    except (DriveError, ManifestError) as exc:
        summary.warnings.append(
            f"No se pudieron consultar los PNG remotos anteriores: {exc}"
        )
        return

    for item in items:
        entry = manifest.get(item.result.output_name)

        if entry is not None:
            item.related = uploads.related_remote_results(
                entry, summary.target.folder.id, grouped.get(item.file_id, [])
            )


@dataclass
class _Context:
    read: object
    write: object | None
    listing: queries.ImageListing
    downloads_dir: Path
    output_dir: Path
    model_name: str
    options: OutputOptions
    max_file_bytes: int
    dry_run: bool
    execute: object
    sleep: object
    on_event: object

    def event(self, stage):
        if self.on_event is None:
            return None
        return lambda *details: self.on_event(stage, *details)


GENERATED_REASON = "PNG generado por esta aplicación"


def _exclude_generated(listing: queries.ImageListing, output_dir: Path) -> queries.ImageListing:
    """
    El listado sin los PNG que subió la aplicación (registro de subidas):
    por su ID remoto o por su contenido (MD5). Solo lee.
    """
    ids, md5s = uploads.uploaded_results(output_dir)
    images, skipped = [], list(listing.skipped)

    for image in listing.images:
        if image.id in ids or (image.md5_checksum is not None and image.md5_checksum in md5s):
            skipped.append(queries.SkippedFile(image.id, image.name, GENERATED_REASON))
        else:
            images.append(image)

    return queries.ImageListing(folder=listing.folder, images=images, skipped=skipped)


def _check_local_dir(path: Path, label: str) -> None:
    if path.exists() and not path.is_dir():
        raise NotADirectoryError(f"{label} no es una carpeta: {path}")


def sync(
    read_service,
    write_service,
    *,
    folder_id: str,
    downloads_dir: Path,
    output_dir: Path,
    target_folder_id: str,
    limit: int | None,
    model_name: str = DEFAULT_MODEL,
    options: OutputOptions | None = None,
    max_file_bytes: int = downloads.DEFAULT_MAX_FILE_BYTES,
    dry_run: bool = False,
    execute=None,
    sleep=None,
    on_event=None,
) -> SyncSummary:
    """
    Sincroniza los originales de folder_id con la carpeta de destino.

    limit=None procesa todos los originales (--all). write_service puede
    ser None solo en la vista previa. Antes de empezar lanza los errores
    de validación (ValueError, NotADirectoryError, ManifestError,
    ForeignFolderError) y los de Drive del listado o del destino
    (incluido DriveWriteAccessError en una ejecución real). Una etapa
    que se detiene queda en SyncSummary.stopped.

    on_event(etapa, *detalle) recibe el resultado de cada imagen en cada
    etapa, tal como lo informa esa etapa.
    """
    if limit is not None and limit < 1:
        raise ValueError("El límite debe ser al menos 1.")

    queries.validate_file_id(folder_id)
    queries.validate_file_id(target_folder_id)

    if write_service is None and not dry_run:
        raise DriveAuthError(
            "Falta la autorización de escritura (drive.file). Autorice con: "
            f"{AUTH_WRITE_COMMAND}"
        )

    options = options or OutputOptions()
    execute = execute or client.execute
    downloads_dir = Path(downloads_dir)
    output_dir = Path(output_dir)

    # 1. Validación local, antes de cualquier consulta.
    _check_local_dir(downloads_dir, "La carpeta de descargas")
    _check_local_dir(output_dir, "La carpeta de resultados")
    local_processing.check_output_folder(output_dir, folder_id)

    # 2. Carpetas de Drive y permiso de escritura en el destino.
    listing = queries.list_images(
        read_service, folder_id, originals_only=True, execute=execute
    )
    target = uploads.check_target(
        read_service, write_service, target_folder_id,
        dry_run=dry_run, execute=execute,
    )

    # Los PNG que subió la aplicación nunca son originales.
    listing = _exclude_generated(listing, output_dir)

    # 3. Una sola selección, con el límite aplicado una vez.
    summary = SyncSummary(folder=listing.folder, target=target, dry_run=dry_run)
    summary.plan = [PlanItem(image) for image in downloads.select_images(listing, limit)]

    skipped = Counter(item.reason for item in listing.skipped)
    for reason, count in sorted(skipped.items()):
        summary.warnings.append(f"Archivos de Drive no incluidos ({reason}): {count}.")

    ctx = _Context(
        read=read_service,
        write=write_service if target.write_access is not False else None,
        listing=listing,
        downloads_dir=downloads_dir,
        output_dir=output_dir,
        model_name=model_name,
        options=options,
        max_file_bytes=max_file_bytes,
        dry_run=dry_run,
        execute=execute,
        sleep=sleep,
        on_event=on_event,
    )

    if not summary.plan:
        return summary

    # 4-6. Cada etapa empieza solo si la anterior no se detuvo.
    if _download_stage(ctx, summary) and _processing_stage(ctx, summary):
        _upload_stage(ctx, summary)

    if summary.stopped is None:
        _report_related(ctx, summary)

    # Vista previa: lo ya vigente queda completo; lo demás, planificado.
    for item in summary.plan:
        if item.status == NOT_STARTED and summary.stopped is None and item.actions:
            item.status = PLANNED

    return summary
