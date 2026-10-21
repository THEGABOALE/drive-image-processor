
import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter

from PIL import Image

from .background_removal import SUPPORTED_MODELS, prepare_model
from .batch import SUPPORTED_EXTENSIONS
from .diagnostics import diagnose_image


@dataclass
class BenchmarkRow:
    filename: str
    model: str
    status: str
    seconds: float | None
    coverage_percent: float | None
    bounding_box: tuple[int, int, int, int] | None
    error: str = ""


def evaluate_folder(
    input_dir: Path,
    output_dir: Path,
    model_names: list[str],
) -> tuple[list[BenchmarkRow], dict[str, float]]:

    input_dir = Path(input_dir)
    output_dir = Path(output_dir)

    if not input_dir.is_dir():
        raise ValueError("No existe la carpeta de origen")

    if not model_names:
        raise ValueError("Debes seleccionar un modelo")

    models = list(dict.fromkeys(model_names))

    for model in models:
        if model not in SUPPORTED_MODELS:
            raise ValueError(f"Modelo no permitido: {model}")

    images = sorted(
        [
            path
            for path in input_dir.iterdir()
            if path.is_file()
            and path.suffix.lower() in SUPPORTED_EXTENSIONS
            and not path.name.upper().startswith("TRANSFORMADA_")
        ],
        key=lambda path: path.name.casefold(),
    )

    if not images:
        raise ValueError("No se encontraron imágenes compatibles")

    # Un benchmark nunca sobrescribe una ejecución anterior.
    if output_dir.exists():
        if not output_dir.is_dir() or any(output_dir.iterdir()):
            raise ValueError(
                "La carpeta de resultados debe estar vacía"
            )

    output_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    initialization_times = {}

    for model in models:
        print(f"\n[MODELO] {model}")

        model_dir = output_dir / model
        model_dir.mkdir(exist_ok=True)

        # Medir carga inicial por separado.
        start = perf_counter()

        try:
            prepare_model(model)
        except Exception as exc:
            initialization_times[model] = round(
                perf_counter() - start, 3
            )

            for path in images:
                rows.append(
                    BenchmarkRow(
                        filename=path.name,
                        model=model,
                        status="error",
                        seconds=None,
                        coverage_percent=None,
                        bounding_box=None,
                        error=f"Carga de modelo: {exc}",
                    )
                )

            print(f"[ERROR] No se pudo cargar {model}")
            continue

        initialization_times[model] = round(
            perf_counter() - start, 3
        )

        print(
            f"Carga inicial: "
            f"{initialization_times[model]:.3f}s"
        )

        for path in images:
            # Incluir extensión para evitar colisiones
            # entre, por ejemplo, producto.jpg y producto.png.
            safe_name = (
                f"{path.stem}__{path.suffix[1:].lower()}"
            )

            final_path = (
                model_dir /
                f"TRANSFORMADA_{safe_name}.png"
            )

            comparison_path = (
                model_dir /
                f"COMPARACION_{safe_name}.png"
            )

            try:
                start = perf_counter()

                with Image.open(path) as image:
                    result = diagnose_image(
                        image,
                        model_name=model,
                    )

                elapsed = round(
                    perf_counter() - start, 3
                )

                # Guardado exclusivo: sin sobrescrituras.
                with final_path.open("xb") as file:
                    result.final.save(file, format="PNG")

                with comparison_path.open("xb") as file:
                    result.comparison.save(file, format="PNG")

                rows.append(
                    BenchmarkRow(
                        filename=path.name,
                        model=model,
                        status="ok",
                        seconds=elapsed,
                        coverage_percent=round(
                            result.coverage_percent, 3
                        ),
                        bounding_box=result.bounding_box,
                    )
                )

                print(
                    f"[OK] {path.name}: {elapsed:.3f}s"
                )

            except Exception as exc:
                # Evitar pares incompletos de archivos.
                final_path.unlink(missing_ok=True)
                comparison_path.unlink(missing_ok=True)

                rows.append(
                    BenchmarkRow(
                        filename=path.name,
                        model=model,
                        status="error",
                        seconds=None,
                        coverage_percent=None,
                        bounding_box=None,
                        error=str(exc),
                    )
                )

                print(f"[ERROR] {path.name}: {exc}")

    save_reports(
        output_dir,
        rows,
        initialization_times,
    )

    return rows, initialization_times


def save_reports(
    output_dir: Path,
    rows: list[BenchmarkRow],
    initialization_times: dict[str, float],
) -> None:

    # Reporte JSON con todos los datos.
    report = {
        "model_initialization_seconds": initialization_times,
        "results": [asdict(row) for row in rows],
    }

    with (output_dir / "reporte.json").open(
        "x", encoding="utf-8"
    ) as file:
        json.dump(
            report,
            file,
            indent=2,
            ensure_ascii=False,
        )

    # Reporte CSV para Excel.
    with (output_dir / "reporte.csv").open(
        "x",
        newline="",
        encoding="utf-8-sig",
    ) as file:

        writer = csv.writer(file)

        writer.writerow([
            "Archivo",
            "Modelo",
            "Estado",
            "Tiempo (s)",
            "Cobertura (%)",
            "Bounding Box",
            "Error",
        ])

        for row in rows:
            writer.writerow([
                row.filename,
                row.model,
                row.status,
                row.seconds,
                row.coverage_percent,
                row.bounding_box,
                row.error,
            ])
