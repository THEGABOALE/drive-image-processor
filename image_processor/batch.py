
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from .atomic_write import remove_stale_temp_files, save_png_exclusive
from .background_removal import prepare_model
from .manifest import (
    COMPLETED,
    PENDING,
    Manifest,
    ManifestEntry,
    ManifestError,
    ResultIdentity,
    SourceRecord,
    file_sha256,
    local_source_id,
    now_utc,
)
from .naming import (  # noqa: F401 (se reexportan)
    OUTPUT_PREFIX,
    SUPPORTED_EXTENSIONS,
    expected_format,
    is_eligible_source,
    name_key,
    output_name,
    plan_outputs,
    with_revision,
)
from .pipeline import PIPELINE_VERSION, process_image
from .processing import OutputOptions


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


@dataclass(frozen=True)
class _Decision:
    action: str  # skip, process, recover, failed, unverified
    output_name: str | None = None
    message: str | None = None
    warning: str | None = None


def _discover_sources(input_dir: Path) -> list[Path]:
    """Originales elegibles, en el orden del sistema de archivos."""

    return [
        path
        for path in input_dir.iterdir()
        if path.is_file() and is_eligible_source(path)
    ]


def _describe_output(path: Path):
    """(tamaño, modo) de un resultado existente, o None si no se lee."""

    try:
        with Image.open(path) as image:
            return image.size, image.mode
    except Exception:
        return None


def _is_complete_png(path: Path, options: OutputOptions) -> bool:
    """PNG legible por completo y con el formato exacto de la variante."""

    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                return False

            if (image.size, image.mode) != expected_format(options):
                return False

            image.load()
    except Exception:
        return False

    return True


def _check_registered(entry, output_dir, options, exists) -> _Decision:
    """Estado de un resultado que el manifiesto atribuye a esta identidad."""

    name = entry.output_name
    path = output_dir / name

    if entry.status == PENDING:
        # Interrupción previa: antes o después de publicar el PNG.
        if not exists(name):
            return _Decision("process", name)

        # El destino estaba libre y la publicación es exclusiva:
        # si el PNG está completo, lo publicó esta aplicación.
        if _is_complete_png(path, options):
            return _Decision("recover", name)

        return _Decision(
            "failed",
            message=(
                f"{name} quedó pendiente de registro y no es un PNG "
                "completo de esta variante. No se adopta ni se "
                "sobrescribe."
            ),
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
    options,
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
    3. Si el nombre base pertenece a otra identidad (otro contenido,
       modelo o versión), se usa el nombre de revisión.
    4. Si el nombre base existe sin registro, es un resultado
       histórico sin verificar: se conserva y no se da por válido.
    """
    revision_name = with_revision(base_name, identity.revision())

    for name in (base_name, revision_name):
        entry = manifest.get(name)

        if entry is not None and entry.identity == identity:
            return _check_registered(entry, output_dir, options, exists)

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
            f"{base_name}: resultado histórico sin verificar; se "
            f"conserva y se genera {revision_name}."
        )

    elif (
        name_key(base_entry.source.name) == name_key(source.name)
        and base_entry.identity.source_sha256 != identity.source_sha256
    ):
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


def _restore(manifest, name, previous, summary) -> None:
    """Deshace una entrada pendiente que no llegó a completarse."""

    if previous is None:
        manifest.remove(name)
    else:
        manifest.put(previous)

    try:
        manifest.save()
    except ManifestError as exc:
        summary.warnings.append(
            f"{name}: no se pudo actualizar el manifiesto ({exc}). "
            "La próxima ejecución lo volverá a intentar."
        )


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

    if options is None:
        options = OutputOptions()

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

    output_dir.mkdir(parents=True, exist_ok=True)

    summary = BatchResult()
    pending = []

    # Restos de ejecuciones interrumpidas.
    summary.temp_removed = remove_stale_temp_files(
        output_dir, prefix=OUTPUT_PREFIX
    )

    manifest = Manifest.load(output_dir)

    existing_names = [path.name for path in output_dir.iterdir()]
    existing = {name_key(name) for name in existing_names}

    def exists(name: str) -> bool:
        # Un resultado con otras mayúsculas también cuenta.
        return name_key(name) in existing or (output_dir / name).exists()

    # Todos los nombres base se deciden antes de procesar.
    plan = plan_outputs(
        _discover_sources(input_dir),
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
            summary.failed += 1
            summary.errors.append(f"{source.name}: {entry.error}")
            continue

        try:
            source_sha256 = file_sha256(source)
            source_size = source.stat().st_size
        except OSError as exc:
            summary.failed += 1
            summary.errors.append(f"{source.name}: {exc}")
            continue

        identity = ResultIdentity(
            source_sha256=source_sha256,
            model=model_name,
            width=options.width,
            height=options.height,
            background=options.background,
            pipeline_version=PIPELINE_VERSION,
        )

        record = SourceRecord(
            kind="local",
            id=local_source_id(source, output_dir),
            name=source.name,
            size=source_size,
            sha256=source_sha256,
        )

        decision = _decide(
            source,
            entry.output_path.name,
            identity,
            options,
            manifest,
            output_dir,
            exists,
            regenerate_unverified,
        )

        if decision.warning is not None:
            summary.warnings.append(decision.warning)

        if decision.action == "skip":
            summary.skipped += 1

        elif decision.action == "failed":
            summary.failed += 1
            summary.errors.append(f"{source.name}: {decision.message}")

        elif decision.action == "unverified":
            summary.unverified += 1
            summary.warnings.append(decision.message)

        elif decision.action == "recover":
            name = decision.output_name
            registered = manifest.get(name)

            try:
                output_sha256 = file_sha256(output_dir / name)
                manifest.put(
                    ManifestEntry(
                        output_name=registered.output_name,
                        status=COMPLETED,
                        identity=registered.identity,
                        source=registered.source,
                        output_sha256=output_sha256,
                        recorded_at=now_utc(),
                    )
                )
                manifest.save()
            except (OSError, ManifestError) as exc:
                manifest.put(registered)
                summary.failed += 1
                summary.errors.append(f"{source.name}: {exc}")
                continue

            summary.recovered += 1
            summary.warnings.append(
                f"{name}: se completó su registro, interrumpido en "
                "una ejecución anterior."
            )

        else:
            pending.append((source, decision.output_name, identity, record))

    # Una sola inicialización por lote, antes de procesar.
    if pending:
        prepare_model(model_name)

    for source, name, identity, record in pending:
        output_path = output_dir / name
        previous = manifest.get(name)

        # Primera fase: se registra la intención antes de publicar.
        manifest.put(
            ManifestEntry(
                output_name=name,
                status=PENDING,
                identity=identity,
                source=record,
                output_sha256=None,
                recorded_at=now_utc(),
            )
        )

        try:
            manifest.save()
        except ManifestError as exc:
            if previous is None:
                manifest.remove(name)
            else:
                manifest.put(previous)

            summary.failed += 1
            summary.errors.append(f"{source.name}: {exc}")
            print(f"[ERROR] {source.name}")
            continue

        try:
            with Image.open(source) as image:
                result = process_image(
                    image,
                    model_name=identity.model,
                    options=options,
                )

            # Publicación atómica: nunca sobrescribe
            # ni deja resultados parciales.
            save_png_exclusive(result, output_path)

        except FileExistsError:
            _restore(manifest, name, previous, summary)
            summary.failed += 1
            summary.errors.append(
                f"{source.name}: apareció {name} sin registro durante "
                "el lote. No se sobrescribe."
            )
            print(f"[ERROR] {source.name}")
            continue

        except Exception as exc:
            _restore(manifest, name, previous, summary)
            summary.failed += 1
            summary.errors.append(f"{source.name}: {exc}")
            print(f"[ERROR] {source.name}")
            continue

        # Segunda fase: el PNG está publicado; se completa el registro.
        try:
            manifest.put(
                ManifestEntry(
                    output_name=name,
                    status=COMPLETED,
                    identity=identity,
                    source=record,
                    output_sha256=file_sha256(output_path),
                    recorded_at=now_utc(),
                )
            )
            manifest.save()
        except (OSError, ManifestError) as exc:
            summary.warnings.append(
                f"{name}: se publicó, pero no se pudo completar su "
                f"registro ({exc}). Se recuperará en la próxima ejecución."
            )

        summary.processed += 1
        print(f"[OK] {source.name}")

    return summary
