
import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from .atomic_write import remove_stale_temp_files, save_png_exclusive
from .background_removal import prepare_model
from .errors import EmptySegmentationError
from .manifest import (
    COMPLETED,
    LOCAL_KIND,
    PENDING,
    Manifest,
    ManifestEntry,
    ManifestError,
    ResultIdentity,
    SourceRecord,
    file_sha256,
    identity_key,
    local_source_id,
    now_utc,
)
from .naming import (  # noqa: F401 (se reexportan)
    OUTPUT_PREFIX,
    SUPPORTED_EXTENSIONS,
    is_eligible_source,
    name_key,
    output_name,
    plan_outputs,
    with_revision,
)
from .pipeline import PIPELINE_VERSION, process_image
from .processing import OutputOptions


# Estado de cada imagen del lote (ItemResult.status).
PROCESSED = "processed"
VERIFIED = "verified"
RECOVERED = "recovered"
UNVERIFIED = "unverified"
FAILED = "failed"
SEGMENTATION_FAILED = "segmentation_failed"
PLANNED = "planned"


@dataclass(frozen=True)
class ItemResult:
    source: Path
    status: str
    output_name: str | None = None
    message: str | None = None
    # False: el PNG se publicó, pero su registro quedó pendiente.
    registered: bool = True


@dataclass
class BatchResult:
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    temp_removed: int = 0
    warnings: list[str] = field(default_factory=list)
    unverified: int = 0
    recovered: int = 0
    # PNG publicados cuyo registro quedó pendiente de recuperación.
    unregistered: int = 0
    # Fallos por segmentación vacía (también cuentan en failed).
    segmentation_failed: int = 0
    # Vista previa (dry_run): imágenes que se procesarían.
    planned: int = 0
    # Detalle por imagen (con rutas): no interviene al comparar lotes.
    items: list[ItemResult] = field(default_factory=list, compare=False)


@dataclass(frozen=True)
class _Decision:
    action: str  # skip, process, recover, failed, unverified
    output_name: str | None = None
    message: str | None = None
    warning: str | None = None
    # Pendiente cuyo archivo no coincide con la evidencia registrada.
    unproven: bool = False


class SourceChangedError(RuntimeError):
    """El original cambió después de verificarse: no se procesa."""


class _EncodedPNG:
    """PNG ya codificado: save_png_exclusive lo publica byte a byte."""

    def __init__(self, data: bytes):
        self.data = data

    def save(self, fp, format=None):
        fp.write(self.data)


def _discover_sources(input_dir: Path) -> list[Path]:
    """Originales elegibles, en el orden del sistema de archivos."""

    return [
        path
        for path in input_dir.iterdir()
        if path.is_file() and is_eligible_source(path)
    ]


def _local_record(source: Path, output_dir: Path) -> SourceRecord:
    return SourceRecord(
        kind=LOCAL_KIND,
        id=local_source_id(source, output_dir),
        name=source.name,
        size=source.stat().st_size,
        sha256=file_sha256(source),
    )


def result_identity(
    record: SourceRecord,
    model_name: str,
    options: OutputOptions,
) -> ResultIdentity:
    """Identidad del resultado de un original con esta configuración."""

    return ResultIdentity(
        source_kind=record.kind,
        source_key=identity_key(record.kind, record.id),
        source_sha256=record.sha256,
        model=model_name,
        width=options.width,
        height=options.height,
        background=options.background,
        pipeline_version=PIPELINE_VERSION,
    )


def _read_verified(source: Path, record: SourceRecord) -> bytes:
    """
    Bytes del original, solo si son los que se registraron. Se
    procesan estos mismos bytes: nunca un archivo cambiado entre la
    verificación y el procesamiento.
    """
    data = source.read_bytes()

    if hashlib.sha256(data).hexdigest() != record.sha256:
        raise SourceChangedError(
            "el original cambió después de verificarse; no se procesa."
        )

    return data


def _describe_output(path: Path):
    """(tamaño, modo) de un resultado existente, o None si no se lee."""

    try:
        with Image.open(path) as image:
            return image.size, image.mode
    except Exception:
        return None


def _check_registered(entry, output_dir, exists) -> _Decision:
    """Estado de un resultado que el manifiesto atribuye a esta identidad."""

    name = entry.output_name
    path = output_dir / name

    if entry.status == PENDING:
        # Interrupción previa: antes o después de publicar el PNG.
        if not exists(name):
            return _Decision("process", name)

        # Solo se adopta el archivo cuyos bytes coinciden con el
        # SHA-256 registrado antes de publicarlo.
        try:
            actual = file_sha256(path)
        except OSError:
            actual = None

        if entry.output_sha256 is not None and actual == entry.output_sha256:
            return _Decision("recover", name)

        return _Decision(
            "unverified",
            name,
            message=(
                f"{name} quedó pendiente de registro, pero su contenido "
                "no coincide con el resultado que se iba a publicar. "
                "Se conserva y no se da por válido."
            ),
            unproven=True,
        )

    if not exists(name):
        return _Decision(
            "process",
            name,
            warning=f"{name}: el resultado registrado no existe; se regenera.",
        )

    try:
        actual = file_sha256(path)
    except OSError as exc:
        return _Decision("failed", message=f"no se pudo leer {name}: {exc}")

    if actual == entry.output_sha256:
        return _Decision("skip", name)

    return _Decision(
        "failed",
        message=(
            f"el resultado {name} fue modificado fuera del procesador "
            "(su SHA-256 no coincide con el registrado). No se "
            "sobrescribe: restáurelo o muévalo para regenerarlo."
        ),
    )


def _decide(
    source,
    base_name,
    identity,
    manifest,
    output_dir,
    exists,
    regenerate_unverified,
) -> _Decision:
    """
    Política de reprocesamiento de una imagen.

    1. Si el manifiesto registra esta identidad con el nombre base o
       con su nombre de revisión, se verifica ese resultado.
    2. Si el nombre base está libre, se usa.
    3. Si el nombre base pertenece a otra identidad (otro original,
       otro contenido, modelo o versión), se usa el nombre de revisión.
    4. Si el nombre base existe sin registro (o con un registro
       pendiente que no se puede demostrar), es un resultado sin
       verificar: se conserva y no se da por válido.
    """
    revision_name = with_revision(base_name, identity.revision())

    for name in (base_name, revision_name):
        entry = manifest.get(name)

        if entry is None or entry.identity != identity:
            continue

        decision = _check_registered(entry, output_dir, exists)

        if not (
            decision.unproven
            and regenerate_unverified
            and name_key(name) == name_key(base_name)
        ):
            return decision

        # El archivo del nombre base no se puede atribuir: se olvida
        # la entrada pendiente y se trata como histórico sin verificar.
        manifest.remove(name)

    base_entry = manifest.get(base_name)
    note = None

    if base_entry is None:
        if not exists(base_name):
            return _Decision("process", base_name)

        if not regenerate_unverified:
            return _Decision(
                "unverified",
                message=(
                    f"{source.name}: el resultado existente {base_name} "
                    "no tiene procedencia verificable (no figura en el "
                    "manifiesto). Se conserva y no se da por válido. "
                    "Revíselo, o use --regenerate-unverified para "
                    "generar una versión verificada con otro nombre."
                ),
            )

        note = (
            f"{base_name}: resultado sin verificar; se conserva y se "
            f"genera {revision_name}."
        )

    elif base_entry.identity.source_key != identity.source_key or (
        base_entry.identity.source_kind != identity.source_kind
    ):
        note = (
            f"{base_name} pertenece a otro original "
            f"({base_entry.source.id}); para {source.name} se genera "
            f"{revision_name}."
        )

    elif base_entry.identity.source_sha256 != identity.source_sha256:
        note = (
            f"{source.name}: el original cambió desde que se generó "
            f"{base_name}; se conserva y se genera {revision_name}."
        )

    if manifest.get(revision_name) is not None:
        return _Decision(
            "failed",
            message=(
                f"el nombre de revisión {revision_name} ya está "
                "registrado para otro resultado."
            ),
        )

    if exists(revision_name):
        return _Decision(
            "unverified",
            message=(
                f"{source.name}: existe {revision_name} sin registro en "
                "el manifiesto. Se conserva y no se da por válido."
            ),
        )

    return _Decision("process", revision_name, warning=note)


def process_folder(
    input_dir: Path,
    output_dir: Path | None = None,
    model_name: str = "isnet-general-use",
    options: OutputOptions | None = None,
    regenerate_unverified: bool = False,
) -> BatchResult:
    """
    Procesa las imágenes de una carpeta.

    options define dimensiones y fondo (por defecto, 400 x 400
    blanco). Los nombres base se planifican antes de procesar,
    según naming.plan_outputs(), y el manifiesto de la carpeta de
    destino decide si cada resultado ya está verificado.

    Un resultado existente sin registro se informa como "sin
    verificar" y no se procesa, salvo que regenerate_unverified
    pida generar una versión verificada con otro nombre.

    Origen y destino pueden ser la misma carpeta.
    No se admiten lotes concurrentes sobre el mismo destino.

    El modelo se inicializa una sola vez y solo si hay
    imágenes pendientes. Si falla, se lanza
    ModelInitializationError antes de procesar fotografías.
    Un manifiesto dañado lanza ManifestError antes de procesar.
    """

    input_dir = Path(input_dir)

    if not input_dir.is_dir():
        raise NotADirectoryError(
            f"No existe la carpeta: {input_dir}"
        )

    output_dir = (
        Path(output_dir)
        if output_dir is not None
        else input_dir
    )

    return process_sources(
        _discover_sources(input_dir),
        output_dir,
        describe=lambda source: _local_record(source, output_dir),
        model_name=model_name,
        options=options,
        regenerate_unverified=regenerate_unverified,
    )


def process_sources(
    sources: list[Path],
    output_dir: Path,
    describe,
    model_name: str = "isnet-general-use",
    options: OutputOptions | None = None,
    regenerate_unverified: bool = False,
    dry_run: bool = False,
    on_item=None,
) -> BatchResult:
    """
    Núcleo del lote, común a todos los orígenes (ver process_folder).

    describe(source) devuelve el SourceRecord del original: su
    procedencia y el SHA-256 que lo identifica. Los bytes se vuelven
    a comprobar contra ese SHA-256 justo antes de procesarlos.

    on_item(ItemResult) recibe el estado de cada imagen; sin él, se
    informa por pantalla como siempre.

    dry_run decide qué se haría sin inicializar el modelo ni
    escribir nada en el destino (tampoco limpia temporales).
    """

    if options is None:
        options = OutputOptions()

    output_dir = Path(output_dir)

    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    summary = BatchResult()
    pending = []

    # Resultados publicados cuyo registro completo no se guardó.
    unsaved: list[str] = []

    def report(source, status, name=None, message=None, echo=None):
        item = ItemResult(source, status, name, message)
        summary.items.append(item)

        if on_item is not None:
            on_item(item)
        elif echo is not None:
            print(echo)

    def fail(source, message, status=FAILED, echo=False):
        summary.failed += 1
        summary.errors.append(f"{source.name}: {message}")
        report(
            source,
            status,
            message=message,
            echo=f"[ERROR] {source.name}" if echo else None,
        )

    if dry_run:
        exists_on_disk = output_dir.is_dir()
        manifest = (
            Manifest.load(output_dir, clean=False)
            if exists_on_disk
            else Manifest(output_dir)
        )
        existing_names = (
            [path.name for path in output_dir.iterdir()]
            if exists_on_disk
            else []
        )
    else:
        # Restos de ejecuciones interrumpidas.
        summary.temp_removed = remove_stale_temp_files(
            output_dir, prefix=OUTPUT_PREFIX
        )

        manifest = Manifest.load(output_dir)
        existing_names = [path.name for path in output_dir.iterdir()]

    def save_manifest() -> None:
        manifest.save()
        # Cada guardado escribe el estado completo en memoria.
        unsaved.clear()

    def restore(name, previous) -> None:
        """Deshace una entrada que no llegó a completarse."""

        if previous is None:
            manifest.remove(name)
        else:
            manifest.put(previous)

        try:
            save_manifest()
        except ManifestError as exc:
            summary.warnings.append(
                f"{name}: no se pudo actualizar el manifiesto ({exc}). "
                "La próxima ejecución lo volverá a intentar."
            )

    existing = {name_key(name) for name in existing_names}

    def exists(name: str) -> bool:
        # Un resultado con otras mayúsculas también cuenta.
        return name_key(name) in existing or (output_dir / name).exists()

    # Todos los nombres base se deciden antes de procesar.
    plan = plan_outputs(
        sources,
        output_dir,
        options,
        existing_names,
        describe_existing=lambda name: _describe_output(output_dir / name),
        known_names=manifest.known_names(),
    )

    summary.warnings.extend(plan.warnings)

    for entry in plan.entries:
        source = entry.source

        if entry.error is not None:
            fail(source, entry.error)
            continue

        try:
            record = describe(source)
        except OSError as exc:
            fail(source, str(exc))
            continue

        identity = result_identity(record, model_name, options)

        decision = _decide(
            source,
            entry.output_path.name,
            identity,
            manifest,
            output_dir,
            exists,
            regenerate_unverified,
        )

        if decision.warning is not None:
            summary.warnings.append(decision.warning)

        if decision.action == "skip":
            summary.skipped += 1
            report(source, VERIFIED, decision.output_name)

        elif decision.action == "failed":
            fail(source, decision.message)

        elif decision.action == "unverified":
            summary.unverified += 1
            summary.warnings.append(decision.message)
            report(source, UNVERIFIED, decision.output_name, decision.message)

        elif decision.action == "recover":
            name = decision.output_name
            registered = manifest.get(name)

            if not dry_run:
                manifest.put(
                    ManifestEntry(
                        output_name=registered.output_name,
                        status=COMPLETED,
                        identity=registered.identity,
                        source=registered.source,
                        output_sha256=registered.output_sha256,
                        recorded_at=now_utc(),
                    )
                )

                try:
                    save_manifest()
                except ManifestError as exc:
                    manifest.put(registered)
                    fail(source, str(exc))
                    continue

            summary.recovered += 1
            message = (
                f"{name}: se completó su registro, interrumpido en "
                "una ejecución anterior."
            )
            summary.warnings.append(message)
            report(source, RECOVERED, name, message)

        else:
            pending.append((source, decision.output_name, identity, record))

    if dry_run:
        for source, name, _, _ in pending:
            summary.planned += 1
            report(source, PLANNED, name)

        return summary

    # Una sola inicialización por lote, antes de procesar.
    if pending:
        prepare_model(model_name)

    for source, name, identity, record in pending:
        output_path = output_dir / name
        previous = manifest.get(name)

        try:
            data = _read_verified(source, record)

            with Image.open(io.BytesIO(data)) as image:
                image.load()
                # Ya decodificada: se conserva la ruta del original,
                # como al abrirla desde el disco.
                image.filename = str(source)

                result = process_image(
                    image,
                    model_name=identity.model,
                    options=options,
                )

            # Se codifica en memoria para conocer el SHA-256 del PNG
            # antes de publicarlo.
            buffer = io.BytesIO()
            result.save(buffer, format="PNG")
            data = buffer.getvalue()

        except EmptySegmentationError as exc:
            summary.segmentation_failed += 1
            fail(source, str(exc), SEGMENTATION_FAILED, echo=True)
            continue

        except Exception as exc:
            fail(source, str(exc), echo=True)
            continue

        output_sha256 = hashlib.sha256(data).hexdigest()

        # Primera fase: se registra la intención y su evidencia.
        manifest.put(
            ManifestEntry(
                output_name=name,
                status=PENDING,
                identity=identity,
                source=record,
                output_sha256=output_sha256,
                recorded_at=now_utc(),
            )
        )

        try:
            save_manifest()
        except ManifestError as exc:
            if previous is None:
                manifest.remove(name)
            else:
                manifest.put(previous)

            fail(source, str(exc), echo=True)
            continue

        try:
            # Publicación atómica: nunca sobrescribe
            # ni deja resultados parciales.
            save_png_exclusive(_EncodedPNG(data), output_path)

        except FileExistsError:
            restore(name, previous)
            fail(
                source,
                f"apareció {name} sin registro durante el lote. "
                "No se sobrescribe.",
                echo=True,
            )
            continue

        except Exception as exc:
            restore(name, previous)
            fail(source, str(exc), echo=True)
            continue

        # Segunda fase: el PNG está publicado; se completa el registro.
        manifest.put(
            ManifestEntry(
                output_name=name,
                status=COMPLETED,
                identity=identity,
                source=record,
                output_sha256=output_sha256,
                recorded_at=now_utc(),
            )
        )

        try:
            save_manifest()
        except ManifestError:
            unsaved.append(name)

        summary.processed += 1
        report(source, PROCESSED, name, echo=f"[OK] {source.name}")

    # Lo que ningún guardado posterior llegó a persistir.
    summary.unregistered = len(unsaved)

    if unsaved:
        pending_names = set(unsaved)
        summary.items = [
            ItemResult(
                item.source,
                item.status,
                item.output_name,
                item.message,
                registered=item.output_name not in pending_names,
            )
            if item.status == PROCESSED
            else item
            for item in summary.items
        ]

    for name in unsaved:
        summary.warnings.append(
            f"{name}: se publicó, pero su registro quedó pendiente. "
            "La próxima ejecución lo recuperará."
        )

    return summary
