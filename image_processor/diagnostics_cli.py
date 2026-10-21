
import argparse
import sys
from pathlib import Path

from PIL import Image

from .background_removal import SUPPORTED_MODELS
from .diagnostics import diagnose_image


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diagnóstico de segmentación de imágenes"
    )

    parser.add_argument(
        "--input",
        type=Path,
        required=True,
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/diagnostics"),
    )

    parser.add_argument(
        "--model",
        choices=SUPPORTED_MODELS,
        default="isnet-general-use",
    )

    parser.add_argument(
        "--threshold",
        type=int,
        default=16,
    )

    args = parser.parse_args(argv)

    if not args.input.is_file():
        print("[ERROR] Imagen no encontrada", file=sys.stderr)
        return 2

    if not 0 <= args.threshold < 255:
        print("[ERROR] Umbral alfa inválido", file=sys.stderr)
        return 2

    name = args.input.stem
    model = args.model
    prefix = f"{name}__{model}"

    files = {
        "recorte": args.output / f"DIAGNOSTICO_{prefix}__recorte.png",
        "mascara": args.output / f"DIAGNOSTICO_{prefix}__mascara.png",
        "final": args.output / f"TRANSFORMADA_{prefix}.png",
        "comparacion": args.output / f"DIAGNOSTICO_{prefix}__comparacion.png",
    }

    if any(path.exists() for path in files.values()):
        print(
            "[ERROR] El diagnóstico ya existe. "
            "No se sobrescribirán archivos.",
            file=sys.stderr,
        )
        return 2

    try:
        with Image.open(args.input) as image:
            result = diagnose_image(
                image,
                model_name=model,
                alpha_threshold=args.threshold,
            )

        args.output.mkdir(parents=True, exist_ok=True)

        images = {
            "recorte": result.cutout,
            "mascara": result.mask,
            "final": result.final,
            "comparacion": result.comparison,
        }

        for key, image in images.items():
            with files[key].open("xb") as target:
                image.save(target, format="PNG")

    except (OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    print("\n===== DIAGNÓSTICO =====")
    print(f"Modelo: {model}")
    print(f"Área visible: {result.coverage_percent:.2f}%")
    print(f"Límites: {result.bounding_box}")

    for path in files.values():
        print(f"[OK] {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
