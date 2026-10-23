from dataclasses import dataclass

from PIL import Image, ImageOps


BACKGROUND_COLORS = {
    "white": (255, 255, 255, 255),
    "transparent": (0, 0, 0, 0),
}

BACKGROUNDS = tuple(BACKGROUND_COLORS)

DEFAULT_WIDTH = 400
DEFAULT_HEIGHT = 400
DEFAULT_BACKGROUND = "white"

# Límites del lienzo final. Un lienzo RGBA de 16 MP
# ocupa unos 64 MB, sin contar las copias intermedias.
MIN_SIDE = 64
MAX_SIDE = 6000
MAX_PIXELS = 16_000_000


@dataclass(frozen=True)
class OutputOptions:
    """Dimensiones y fondo del resultado final."""

    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    background: str = DEFAULT_BACKGROUND

    def __post_init__(self):
        for label, value in (
            ("ancho", self.width),
            ("alto", self.height),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"El {label} debe ser un número entero."
                )

            if not MIN_SIDE <= value <= MAX_SIDE:
                raise ValueError(
                    f"El {label} debe estar entre "
                    f"{MIN_SIDE} y {MAX_SIDE} píxeles."
                )

        if self.width * self.height > MAX_PIXELS:
            raise ValueError(
                "La imagen final no puede superar "
                f"{MAX_PIXELS:,} píxeles."
            )

        if self.background not in BACKGROUNDS:
            raise ValueError(
                f"Fondo no válido: {self.background}. "
                f"Opciones: {', '.join(BACKGROUNDS)}."
            )


def _finish(canvas: Image.Image, options: OutputOptions) -> Image.Image:
    # Fondo blanco: RGB sin alfa. Transparente: RGBA.
    if options.background == "white":
        return canvas.convert("RGB")

    return canvas


def normalize_image(
        image: Image.Image,
        options: OutputOptions | None = None,
        margin: int = 20,
        alpha_threshold: int = 16,
        crop_padding: int = 3,
) -> Image.Image:
    """
    Centra una imagen sobre un lienzo blanco o transparente.

    Utiliza un umbral alfa para ignorar residuos casi transparentes al calcular el recorte.

    Conserva las proporciones del objeto. Devuelve RGB con fondo
    blanco o RGBA con fondo transparente.
    """
    if options is None:
        options = OutputOptions()

    if not isinstance(options, OutputOptions):
        raise TypeError(
            "options debe ser una instancia de OutputOptions."
        )

    width, height = options.width, options.height

    if margin < 0 or margin * 2 >= min(width, height):
        raise ValueError("El margen no es válido.")

    if not 0 <= alpha_threshold < 255:
        raise ValueError(
            "El umbral alfa debe estar entre 0 y 254."
        )

    if crop_padding < 0:
        raise ValueError(
            "El relleno de recorte no puede ser negativo."
        )

    if image.width <= 0 or image.height <= 0:
        raise ValueError(
            "La imagen debe de tener dimensiones positivas."
        )

    has_alpha = (
        "A" in image.getbands()
        or "transparency" in image.info
    )

    source = ImageOps.exif_transpose(image).convert("RGBA")

    if has_alpha:
        alpha = source.getchannel("A")

        # Solo los píxeles suficientemente visibles
        # determinan el área de recorte.
        visable_mask = alpha.point(
            lambda value: (
                255 if value > alpha_threshold else 0
            )
        )

        bbox = visable_mask.getbbox()

        # Si todo el objeto es muy translúcido,
        # usamos el área alfa original.
        if bbox is None:
            bbox = alpha.getbbox()

        if bbox is None:
            blank = Image.new(
                "RGBA",
                (width, height),
                BACKGROUND_COLORS[options.background],
            )
            return _finish(blank, options)

        left, top, right, bottom = bbox

        # Pequeño margen para conservar bordes suaves.
        left = max(0, left - crop_padding)
        top = max(0, top - crop_padding)
        right = min(source.width, right + crop_padding)
        bottom = min(source.height, bottom + crop_padding)

        source = source.crop(
            (left, top, right, bottom)
        )

    available_width = width - (margin * 2)
    available_height = height - (margin * 2)

    scale = min(
        available_width / source.width,
        available_height / source.height
    )

    new_width = max(
        1, round(source.width * scale)
    )
    new_height = max(
        1, round(source.height * scale)
    )

    resized = source.resize(
        (new_width, new_height),
        Image.Resampling.LANCZOS,
    )

    canvas = Image.new(
        "RGBA",
        (width, height),
        BACKGROUND_COLORS[options.background],
    )

    x = (width - new_width) // 2
    y = (height - new_height) // 2

    # Sobre un lienzo transparente, alpha_composite
    # conserva el alfa de la segmentación sin cambios.
    canvas.alpha_composite(resized, (x, y))

    return _finish(canvas, options)
