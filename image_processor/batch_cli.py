import argparse
import sys
from pathlib import Path

from .background_removal import (
    SUPPORTED_MODELS,
    ModelInitializationError,
)
from .batch import process_folder
from .manifest import ManifestError
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
    """
    Códigos de salida:

    0  Todo procesado o verificado.
    1  Alguna imagen falló.
    2  Argumentos o carpetas inválidos.
    3  No se pudo inicializar el modelo.
    4  Sin fallos, pero hay resultados sin verificar.
    5  El manifiesto de destino está dañado o no es compatible.
    6  Sin fallos, pero algún PNG publicado quedó con su registro
       pendiente (se recupera en la siguiente ejecución).

    Si se cumplen varias condiciones, prevalece la primera de esta
    lista: 1, 6 y luego 4.
    """
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

    parser.add_argument(
        "--regenerate-unverified",
        action="store_true",
        help=(
            "Genera una versión verificada, con otro nombre, de las "
            "imágenes cuyo resultado histórico no figura en el "
            "manifiesto. Los archivos históricos se conservan."
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
            regenerate_unverified=args.regenerate_unverified,
        )
    except ManifestError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        print(
            "No se procesó ninguna imagen y el manifiesto no se "
            "modificó. Restáurelo desde una copia o muévalo para "
            "revisarlo.",
            file=sys.stderr,
        )
        return 5
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

    if result.unverified:
        print(f"Sin verificar: {result.unverified}")

    if result.recovered:
        print(f"Recuperadas: {result.recovered}")

    if result.unregistered:
        print(f"Sin registrar: {result.unregistered}")

    if result.errors:
        print("\nErrores encontrados:")
        for error in result.errors:
            print(f"  - {error}", file=sys.stderr)

    if result.warnings:
        print("\nAdvertencias:")
        for warning in result.warnings:
            print(f"  - {warning}", file=sys.stderr)

    if result.failed:
        return 1

    if result.unregistered:
        print(
            "\nHay resultados publicados con el registro pendiente: "
            "no están verificados. Vuelva a ejecutar el lote para "
            "completarlo."
        )
        return 6

    if result.unverified:
        print(
            "\nHay resultados sin verificar: requieren revisión o "
            "--regenerate-unverified."
        )
        return 4

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
