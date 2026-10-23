from PIL import Image

from .background_removal import remove_background
from .errors import EmptySegmentationError
from .processing import OutputOptions, normalize_image


# Versión de la semántica del resultado. Se incrementa cuando un
# cambio en el procesamiento hace que el mismo original, modelo y
# opciones produzcan un PNG distinto; el manifiesto deja entonces
# de considerar válidos los resultados anteriores.
PIPELINE_VERSION = "1"


def _ensure_segmentation_not_empty(
        cutout: Image.Image,
        model_name: str,
) -> None:
    # Se valida el alfa de la segmentación, no el PNG final:
    # un producto blanco sobre fondo blanco parecería vacío.
    # Cualquier alfa distinto de cero cuenta como objeto.
    if cutout.getchannel("A").getbbox() is None:
        raise EmptySegmentationError(
            f"El modelo {model_name} no detectó ningún objeto: "
            "la segmentación está vacía."
        )


def  process_image(
        image: Image.Image,
        model_name: str = "isnet-general-use",
        options: OutputOptions | None = None,
        margin: int = 20,
) -> Image.Image:
    """
    Ejecuta el procesamiento completo.

    1. Eliminación del fondo original.
    2. Rechazo de segmentaciones vacías (EmptySegmentationError).
    3. Centra el objeto sobre un fondo blanco o transparente.
    4. Ajusta las dimensiones finales.
    """

    cutout = remove_background(
        image,
        model_name=model_name,
    )

    _ensure_segmentation_not_empty(cutout, model_name)

    return normalize_image(
        cutout,
        options=options,
        margin=margin,
    )