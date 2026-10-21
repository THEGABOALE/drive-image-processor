from functools import lru_cache

from PIL import Image, ImageOps
from rembg import new_session, remove

SUPPORTED_MODELS = [
    "isnet-general-use",
    "birefnet-general-lite",
    "birefnet-general",
]


class ModelInitializationError(RuntimeError):
    """El modelo de IA no pudo descargarse o inicializarse."""


@lru_cache(maxsize=1)
def _get_session(model_name: str):
    """Reutiliza la sesión del modelo de IA."""

    return new_session(model_name)



def prepare_model(model_name: str) -> None:
    """
    Carga el modelo y deja su sesión preparada.

    Permite medir el tiempo de inicialización
    independientemente del procesamiento.

    Un fallo no queda en caché: la siguiente
    llamada vuelve a intentarlo.
    """
    if model_name not in SUPPORTED_MODELS:
        raise ValueError(
            f"Modelo no permitido: {model_name}"
        )

    try:
        _get_session(model_name)
    except Exception as exc:
        raise ModelInitializationError(
            f"No se pudo inicializar el modelo "
            f"'{model_name}': {exc}"
        ) from exc


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