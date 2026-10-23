from PIL import Image

from .background_removal import remove_background
from .processing import OutputOptions, normalize_image


def  process_image(
        image: Image.Image,
        model_name: str = "isnet-general-use",
        options: OutputOptions | None = None,
        margin: int = 20,
) -> Image.Image:
    """
    Ejecuta el procesamiento completo.

    1. Eliminación del fondo original.
    2. Centra el objeto sobre un fondo blanco o transparente.
    3. Ajusta las dimensiones finales.
    """

    cutout = remove_background(
        image,
        model_name=model_name,
    )

    return normalize_image(
        cutout,
        options=options,
        margin=margin,
    )