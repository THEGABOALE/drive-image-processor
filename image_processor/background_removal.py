from functools import lru_cache

from PIL import Image, ImageOps
from rembg import new_session, remove

SUPPORTED_MODELS = [
    "isnet-general-use",
    "birefnet-general-lite",
    "birefnet-general",
]


@lru_cache(maxsize=1)
def _get_session(model_name: str):
    """Reutiliza la sesión del modelo de IA."""

    return new_session(model_name)


def remove_background(
        image: Image.Image,
        model_name: str = "isnet-general-use",
) -> Image.Image:
    """Elimina el fondo y devuelve una imagen RGBA."""

    if model_name not in SUPPORTED_MODELS:
        raise ValueError(
            f"Modelo no permitido: {model_name}"
        )

    if image.width == 0 or image.height == 0:
        raise ValueError("La imagen no puede estar vacía.")

    source = ImageOps.exif_transpose(image).convert("RGB")

    result = remove(
        source,
        session=_get_session(model_name),
    )

    return result.convert("RGBA")