import argparse
from pathlib import Path

from PIL import Image

from .background_removal import  SUPPORTED_MODELS
from .pipeline import process_image


def main():
    parser = argparse.ArgumentParser(
        description="Precesador de imágenes de productos"
    )

    parser.add_argument(
        "input",
        type=Path,
        help="Ruta de la imagen original",
    )

    parser.add_argument(
        "--model",
        choices=SUPPORTED_MODELS,
        default="isnet-general-use",
    )

    args = parser.parse_args()

    if not args.input.is_file():
        parser.error("No se encontró la imagen original")

    if args.input.stem.upper().startswith("TRANSFORMADA_"):
        parser.error("No se debe de reprocesar una imagen transformada")

    output_dir = Path("outputs")
    output_dir.mkdir(parents=True, exist_ok=True)

    output_path = (
        output_dir / f"TRANSFORMADA_{args.input.stem}.png"
    )

    if output_path.exists():
        parser.error(
            "El resultado ya existe. No se sobreescribirá."
        )

    with Image.open(args.input) as image:
        result = process_image(
            image,
            model_name=args.model,
        )

    result.save(
        output_path,
        format="PNG",
        optimize=True,
    )

    print(f"Imagen procesada: {output_path}")


if __name__ == "__main__":
    main()
