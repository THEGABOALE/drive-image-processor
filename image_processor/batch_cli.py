import argparse
import sys
from pathlib import Path

from .background_removal import SUPPORTED_MODELS
from .batch import process_folder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Procesamiento por lotes de fotografías"
    )

    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Carpeta con la imágenes originales",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Carpeta de destino (por defecto, la misma)",
    )

    parser.add_argument(
        "--model",
        choices=SUPPORTED_MODELS,
        default="isnet-general-use",
        help="Modelo de eliminación de fondo",
    )

    args = parser.parse_args(argv)

    print("\nIniciando procesamiento de imágenes...")

    try:
        result = process_folder(
            input_dir=args.input,
            output_dir=args.output,
            model_name=args.model,
        )
    except (OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    print("\n===== RESUMEN DEL LOTE =====")
    print(f"Procesadas: {result.processed}")
    print(f"Omitidas:   {result.skipped}")
    print(f"Fallidas:   {result.failed}")

    if result.errors:
        print("\nErrores encontrados:")
        for error in result.errors:
            print(f"  - {error}", file=sys.stderr)

    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
