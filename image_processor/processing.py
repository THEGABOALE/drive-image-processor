from PIL import Image, ImageOps


def normalize_image(
        image: Image.Image,
        canvas_size: int = 400,
        margin: int = 20,
        alpha_threshold: int = 16,
        crop_padding: int = 3,
) -> Image.Image:
    """
    Centra una imagen sobre un fondo blanco.
    
    Utiliza un umbral alfa para ignorar residuos casi transparentes al calcular el recorte.
    
    Cambia las proporciones y devuelve RGB.
    """
    if canvas_size <=0:
        raise ValueError(
            "El tamaño del lienzo debe ser positivo."
        )

    if margin < 0 or margin * 2 >= canvas_size:
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
            return Image.new(
                "RGB",
                (canvas_size, canvas_size),
                "white",
            )

        left, top, right, bottom = bbox

        # Pequeño margen para conservar bordes suaves.
        left = max(0, left - crop_padding)
        top = max(0, top - crop_padding)
        right = min(source.width, right + crop_padding)
        bottom = min(source.height, bottom + crop_padding)

        source = source.crop(
            (left, top, right, bottom)
        )

    available_size = canvas_size - (margin * 2)


    scale = min(
        available_size / source.width,
        available_size / source.height
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
        (canvas_size, canvas_size),
        (255, 255, 255, 255),
    )

    x = (canvas_size - new_width) // 2
    y = (canvas_size - new_height) // 2

    canvas.alpha_composite(resized, (x, y))

    return canvas.convert("RGB")
