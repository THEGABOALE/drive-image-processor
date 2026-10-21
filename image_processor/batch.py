
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from .atomic_write import remove_stale_temp_files, save_png_exclusive
from .background_removal import prepare_model
from .pipeline import process_image


SUPPORTED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp"
}


@dataclass
class BatchResult:
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    temp_removed: int = 0


def process_folder(
    input_dir: Path,
    output_dir: Path | None = None,
    model_name: str = "isnet-general-use",
) -> BatchResult:
    """
    Procesa las imágenes de una carpeta.

    Origen y destino pueden ser la misma carpeta.
    No se admiten lotes concurrentes sobre el mismo destino.

    El modelo se inicializa una sola vez y solo si hay
    imágenes pendientes. Si falla, se lanza
    ModelInitializationError antes de procesar fotografías.
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

    output_dir.mkdir(parents=True, exist_ok=True)

    summary = BatchResult()
    reserved_names = set()
    pending = []

    # Restos de ejecuciones interrumpidas.
    summary.temp_removed = remove_stale_temp_files(
        output_dir, prefix="TRANSFORMADA_"
    )

    for file in sorted(input_dir.iterdir()):

        if not file.is_file():
            continue

        if file.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue

        if file.name.upper().startswith("TRANSFORMADA_"):
            continue

        output_path = (
            output_dir / f"TRANSFORMADA_{file.stem}.png"
        )

        # Evita colisiones de nombres en el mismo lote.
        output_key = output_path.name.casefold()

        if output_key in reserved_names:
            summary.failed += 1
            summary.errors.append(
                f"{file.name}: nombre de salida duplicado"
            )
            continue

        reserved_names.add(output_key)

        if output_path.exists():
            summary.skipped += 1
            continue

        pending.append((file, output_path))

    # Una sola inicialización por lote, antes de procesar.
    if pending:
        prepare_model(model_name)

    for file, output_path in pending:
        try:
            with Image.open(file) as image:
                result = process_image(
                    image,
                    model_name=model_name,
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
