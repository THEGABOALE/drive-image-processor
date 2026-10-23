
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from .atomic_write import remove_stale_temp_files, save_png_exclusive
from .background_removal import prepare_model
from .naming import (  # noqa: F401 (se reexportan)
    OUTPUT_PREFIX,
    SUPPORTED_EXTENSIONS,
    is_eligible_source,
    name_key,
    output_name,
    plan_outputs,
)
from .pipeline import process_image
from .processing import OutputOptions


@dataclass
class BatchResult:
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    temp_removed: int = 0
    warnings: list[str] = field(default_factory=list)


def _discover_sources(input_dir: Path) -> list[Path]:
    """Originales elegibles, en el orden del sistema de archivos."""

    return [
        path
        for path in input_dir.iterdir()
        if path.is_file() and is_eligible_source(path)
    ]


def process_folder(
    input_dir: Path,
    output_dir: Path | None = None,
    model_name: str = "isnet-general-use",
    options: OutputOptions | None = None,
) -> BatchResult:
    """
    Procesa las imágenes de una carpeta.

    options define dimensiones y fondo (por defecto, 400 x 400
    blanco). Los destinos se planifican antes de procesar,
    según la política de naming.plan_outputs().

    Origen y destino pueden ser la misma carpeta.
    No se admiten lotes concurrentes sobre el mismo destino.

    El modelo se inicializa una sola vez y solo si hay
    imágenes pendientes. Si falla, se lanza
    ModelInitializationError antes de procesar fotografías.
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

    existing_names = [path.name for path in output_dir.iterdir()]
    existing = {name_key(name) for name in existing_names}

    # Todos los destinos se deciden antes de procesar.
    plan = plan_outputs(
        _discover_sources(input_dir),
        output_dir,
        options,
        existing_names,
    )

    summary.warnings.extend(plan.warnings)

    for entry in plan.entries:
        if entry.error is not None:
            summary.failed += 1
            summary.errors.append(
                f"{entry.source.name}: {entry.error}"
            )
            continue

        # Un resultado previo con otras mayúsculas también cuenta.
        if (
            name_key(entry.output_path.name) in existing
            or entry.output_path.exists()
        ):
            summary.skipped += 1
            continue

        pending.append((entry.source, entry.output_path))

    # Una sola inicialización por lote, antes de procesar.
    if pending:
        prepare_model(model_name)

    for file, output_path in pending:
        try:
            with Image.open(file) as image:
                result = process_image(
                    image,
                    model_name=model_name,
                    options=options,
                )

            # Publicación atómica: nunca sobrescribe
            # ni deja resultados parciales.
            save_png_exclusive(result, output_path)

            summary.processed += 1
            print(f"[OK] {file.name}")

        except FileExistsError:
            summary.skipped += 1

        except Exception as exc:
            summary.failed += 1
            summary.errors.append(
                f"{file.name}: {exc}"
            )
            print(f"[ERROR] {file.name}")

    return summary
