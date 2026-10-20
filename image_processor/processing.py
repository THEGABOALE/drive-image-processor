from PIL import Image, ImageOps


def normalize_image(
        image: Image.Image,
        canvas_size: int = 400,
        margin: int = 20,
) -> Image.Image:
    """
    Centra una imagen sobre un fondo blanco.
    
    Mantiene las proporciones originales y devuelve una imagen RGB de canvas_size x canvas_size.
    
    Si la imagen tiene transparencia, recorta primero los bordes completamente transparentes.
    """
    if canvas_size <=0:
        raise ValueError("El tamaño del lienzo debe ser positivo.")

    if margin < 0 or margin * 2 >= canvas_size:
        raise ValueError("El margen no es válido.")

    has_alpha = (
        "A" in image.getbands()
        or "transparency" in image.info
    )

    source = ImageOps.exif_transpose(image).convert("RGBA")

    if has_alpha:
        bbox = source.getchannel("A").getbbox()

        if bbox is None:
            return Image.new(
                "RGB",
                (canvas_size, canvas_size),
                ("white"),
            )

        source = source.crop(bbox)

    available_size = canvas_size - (margin * 2)

    width, height = source.size

    scale = min(
        available_size / width,
        available_size / height
    )

    new_width = max(1, round(width * scale))
    new_height = max(1, round(height * scale))

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
