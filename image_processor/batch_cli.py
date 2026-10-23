import argparse
import sys
from pathlib import Path

from .background_removal import (
    SUPPORTED_MODELS,
    ModelInitializationError,
)
from .batch import process_folder
from .processing import (
    BACKGROUNDS,
    DEFAULT_BACKGROUND,
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    MAX_SIDE,
    MIN_SIDE,
    OutputOptions,
)


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

    parser.add_argument(
        "--background",
        choices=BACKGROUNDS,
        default=DEFAULT_BACKGROUND,
        help=f"Fondo del resultado (por defecto, {DEFAULT_BACKGROUND})",
    )

    parser.add_argument(
        "--width",
        type=int,
        default=DEFAULT_WIDTH,
        help=(
            f"Ancho en píxeles, entre {MIN_SIDE} y {MAX_SIDE} "
            f"(por defecto, {DEFAULT_WIDTH})"
        ),
    )

    parser.add_argument(
        "--height",
        type=int,
        default=DEFAULT_HEIGHT,
        help=(
            f"Alto en píxeles, entre {MIN_SIDE} y {MAX_SIDE} "
            f"(por defecto, {DEFAULT_HEIGHT})"
        ),
    )

    args = parser.parse_args(argv)

    try:
        options = OutputOptions(
            width=args.width,
            height=args.height,
            background=args.background,
        )
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    print("\nIniciando procesamiento de imágenes...")
    print(
        f"Resultado: {options.width} x {options.height} px, "
        f"fondo {options.background}"
    )

    try:
        result = process_folder(
            input_dir=args.input,
            output_dir=args.output,
            model_name=args.model,
            options=options,
        )
    except ModelInitializationError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        print(
            "No se procesó ninguna imagen. "
            "Verifique la conexión de red o la caché de modelos.",
            file=sys.stderr,
        )
        return 3
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
