from PIL import Image

from .background_removal import remove_background
from .processing import normalize_image


def  process_image(
        image: Image.Image,
        model_name: str = "isnet-general-use",
        canvas_size: int = 400,
        margin: int = 20,
) -> Image.Image:
    """
    Ejecuta el procesamiento completo.
    
    1. Eliminación del fondo original.
    2. Central el objeto sobre un fondo blanco.
    3. Ajusta las dimensiones finales.
    """

    cutout = remove_background(
        image,
        model_name=model_name,
    )

    return normalize_image(
        cutout,
        canvas_size=canvas_size,
        margin=margin,
    )