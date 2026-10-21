
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageOps

from .background_removal import remove_background
from .processing import normalize_image


@dataclass
class DiagnosticResult:
    cutout: Image.Image
    mask: Image.Image
    final: Image.Image
    comparison: Image.Image
    coverage_percent: float
    bounding_box: tuple[int, int, int, int] | None


def _make_panel(
    image: Image.Image,
    background: str = "white",
    resampling: Image.Resampling = Image.Resampling.LANCZOS,
) -> Image.Image:
    """Ajusta una imagen a un panel sin deformarla."""

    fitted = ImageOps.contain(
        image.convert("RGBA"),
        (400, 400),
        method=resampling,
    )

    panel = Image.new("RGBA", (400, 400), background)

    x = (400 - fitted.width) // 2
    y = (400 - fitted.height) // 2

    panel.alpha_composite(fitted, (x, y))

    return panel.convert("RGB")


def diagnose_image(
    image: Image.Image,
    model_name: str = "isnet-general-use",
    alpha_threshold: int = 16,
) -> DiagnosticResult:
    """Analiza el resultado de un modelo de segmentación."""

    if not 0 <= alpha_threshold < 255:
        raise ValueError("Umbral alfa inválido")

    original = ImageOps.exif_transpose(image)

    # Ejecutamos la IA una sola vez.
    cutout = remove_background(
        image,
        model_name=model_name,
    )

    # Conservamos todos los niveles de transparencia.
    mask = cutout.getchannel("A")

    # Máscara binaria solamente para las mediciones.
    visible = mask.point(
        lambda value: 255 if value > alpha_threshold else 0
    )

    bounding_box = visible.getbbox()

    visible_pixels = visible.histogram()[255]
    total_pixels = visible.width * visible.height

    coverage = 100 * visible_pixels / total_pixels

    final = normalize_image(
        cutout,
        alpha_threshold=alpha_threshold,
    )

    panels = [
        _make_panel(original),
        _make_panel(
            mask,
            background="black",
            resampling=Image.Resampling.NEAREST,
        ),
        final,
    ]

    comparison = Image.new(
        "RGB",
        (1200, 432),
        "white",
    )

    draw = ImageDraw.Draw(comparison)

    labels = [
        "Original",
        "Mascara alfa",
        "Resultado 400 x 400",
    ]

    for index, (panel, label) in enumerate(
        zip(panels, labels)
    ):
        x = index * 400

        draw.text(
            (x + 12, 9),
            label,
            fill="black",
        )

        comparison.paste(panel, (x, 32))

    return DiagnosticResult(
        cutout=cutout,
        mask=mask,
        final=final,
        comparison=comparison,
        coverage_percent=coverage,
        bounding_box=bounding_box,
    )
