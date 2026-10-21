
import argparse
import statistics
import sys
from pathlib import Path

from .background_removal import SUPPORTED_MODELS
from .benchmark import evaluate_folder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark de modelos de segmentación"
    )

    parser.add_argument(
        "--input",
        type=Path,
        required=True,
    )

    parser.add_argument(
        "--output",
        type=Path,
        required=True,
    )

    parser.add_argument(
        "--model",
        choices=SUPPORTED_MODELS,
        action="append",
        help="Puede especificarse varias veces",
    )

    args = parser.parse_args(argv)

    models = args.model or [
        "isnet-general-use",
        "birefnet-general-lite",
    ]

    try:
        rows, initialization_times = evaluate_folder(
            args.input,
            args.output,
            models,
        )
    except (OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    print("\n===== RESUMEN DEL BENCHMARK =====")

    for model in dict.fromkeys(models):
        model_rows = [
            row for row in rows if row.model == model
        ]

        successful = [
            row.seconds
            for row in model_rows
            if row.status == "ok" and row.seconds is not None
        ]

        failed = sum(
            row.status != "ok" for row in model_rows
        )

        print(f"\nModelo: {model}")
        print(f"Procesadas: {len(successful)}")
        print(f"Fallidas: {failed}")
        print(
            "Inicialización: "
            f"{initialization_times[model]:.3f}s"
        )

        if successful:
            print(
                "Mediana por imagen: "
                f"{statistics.median(successful):.3f}s"
            )

    print(f"\nReportes guardados en: {args.output}")

    return 1 if any(row.status != "ok" for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
