"""
Imágenes para la pantalla: miniaturas del último trabajo y un lienzo de
ejemplo con el tamaño y el fondo elegidos.

El ejemplo usa la función del motor que centra el producto en el lienzo
(processing.normalize_image), así que se ve igual que un resultado real. El
recorte sale de lo que la cuenta ya procesó: un resultado transparente, o, si
no hay ninguno, una de sus fotos descargadas, recortada una sola vez con el
modelo y guardada. Nada de esto toca Drive ni los registros del motor.
"""

import hashlib
import io
import threading
from pathlib import Path

from image_processor import background_removal
from image_processor.errors import EmptySegmentationError
from image_processor.google_drive import local_processing
from image_processor.naming import SUPPORTED_EXTENSIONS
from image_processor.processing import MIN_SIDE, OutputOptions, normalize_image
from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

THUMBNAIL_MAX_SIDE = 1400
SAMPLE_MAX_SIDE = 1200
CUTOUT_MAX_SIDE = 1600

# El margen del motor, en píxeles del lienzo final.
ENGINE_MARGIN = 20

_CUTOUT_NAME = "cutout.png"

# Lo que cuenta como visible al ubicar el producto (el umbral del motor).
_VISIBLE_ALPHA = 16


class ImageNotAvailable(Exception):
    """No hay una imagen para mostrar."""


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _open(path: Path) -> Image.Image:
    try:
        with Image.open(path) as image:
            image.load()
            return ImageOps.exif_transpose(image)
    except (OSError, UnidentifiedImageError, ValueError):
        raise ImageNotAvailable() from None


def thumbnail(path: Path, *, lossless: bool = False) -> tuple[bytes, str]:
    """
    Una copia reducida. Los resultados (lossless) y lo que tiene
    transparencia van en PNG; las fotos originales, en JPEG.
    """
    image = _open(path)
    image.thumbnail((THUMBNAIL_MAX_SIDE, THUMBNAIL_MAX_SIDE), Image.Resampling.LANCZOS)

    if lossless or "A" in image.getbands():
        return _png(image), "image/png"

    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=88)
    return buffer.getvalue(), "image/jpeg"


def _is_cutout(image: Image.Image) -> bool:
    """¿Tiene transparencia y algo visible?"""

    if "A" not in image.getbands():
        return False
    alpha = image.getchannel("A")
    low, high = alpha.getextrema()
    return low < 255 and high > 0 and alpha.getbbox() is not None


def _visible_box(image: Image.Image):
    alpha = image.getchannel("A").point(lambda value: 255 if value > _VISIBLE_ALPHA else 0)
    return alpha.getbbox()


def _jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


def _newest(paths):
    return sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True)


def _originals(folders: Path):
    for path in folders.glob("*/downloads/*"):
        if (
            path.is_file()
            and not path.name.startswith(".")
            and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ):
            yield path


class SampleSource:
    """El recorte de ejemplo de cada cuenta. Una sola llamada al modelo a la vez."""

    def __init__(self, model_name: str = local_processing.DEFAULT_MODEL):
        self._model_name = model_name
        self._lock = threading.Lock()

    def cutout(self, account_dir: Path) -> Image.Image:
        folders = Path(account_dir) / "folders"

        for path in _newest(folders.glob("*/outputs/*.png")):
            try:
                image = _open(path)
            except ImageNotAvailable:
                continue
            if _is_cutout(image):
                return image.convert("RGBA")

        cache = Path(account_dir) / "sample" / _CUTOUT_NAME

        with self._lock:
            if cache.is_file():
                try:
                    return _open(cache).convert("RGBA")
                except ImageNotAvailable:
                    pass

            for path in _newest(_originals(folders)):
                try:
                    original = _open(path).convert("RGB")
                except ImageNotAvailable:
                    continue

                original.thumbnail((CUTOUT_MAX_SIDE, CUTOUT_MAX_SIDE), Image.Resampling.LANCZOS)

                try:
                    cut = background_removal.remove_background(original, self._model_name)
                except EmptySegmentationError:
                    continue

                if not _is_cutout(cut):
                    continue

                cache.parent.mkdir(parents=True, exist_ok=True)
                cut.save(cache, format="PNG")
                return cut.convert("RGBA")

        raise ImageNotAvailable()

    def aligned(self, original_path: Path, result_path: Path, cache_dir: Path) -> bytes:
        """
        El original con el mismo encuadre que el resultado, para compararlos
        con una línea. El producto se ubica con el modelo; dónde lo deja el
        motor sale de su propia función (normalize_image), comparando el
        producto antes y después. Alrededor queda la foto original.
        """
        stamp = "|".join(
            f"{path}:{path.stat().st_mtime_ns}" for path in (original_path, result_path)
        )
        cache = Path(cache_dir) / f"{hashlib.sha256(stamp.encode()).hexdigest()[:24]}.jpg"

        if cache.is_file():
            return cache.read_bytes()

        with Image.open(result_path) as result:
            width, height = result.size

        original = _open(original_path).convert("RGB")
        original.thumbnail((CUTOUT_MAX_SIDE, CUTOUT_MAX_SIDE), Image.Resampling.LANCZOS)

        with self._lock:
            cut = background_removal.remove_background(original, self._model_name)

        source = _visible_box(cut.convert("RGBA")) if _is_cutout(cut) else None
        placed = normalize_image(
            cut, OutputOptions(width=width, height=height, background="transparent")
        )
        target = _visible_box(placed)

        if source is None or target is None:
            raise ImageNotAvailable()

        scale = (target[2] - target[0]) / (source[2] - source[0])
        dx = target[0] - source[0] * scale
        dy = target[1] - source[1] * scale

        matrix = (1 / scale, 0, -dx / scale, 0, 1 / scale, -dy / scale)
        framed = original.transform(
            (width, height), Image.Transform.AFFINE, matrix, resample=Image.Resampling.BICUBIC
        )
        # Donde la foto no llega, la misma foto desenfocada: sin bandas vacías.
        covered = Image.new("L", original.size, 255).transform(
            (width, height), Image.Transform.AFFINE, matrix, fillcolor=0
        )
        if covered.getextrema()[0] < 255:
            # Un píxel menos de foto en el borde: el bicúbico lo mezcla con negro.
            covered = covered.filter(ImageFilter.MinFilter(3))
            backdrop = ImageOps.fit(original, (width, height), Image.Resampling.BILINEAR)
            backdrop = backdrop.filter(ImageFilter.GaussianBlur(max(width, height) / 25))
            framed = Image.composite(framed, backdrop, covered)
        framed.thumbnail((THUMBNAIL_MAX_SIDE, THUMBNAIL_MAX_SIDE), Image.Resampling.LANCZOS)
        content = _jpeg(framed)

        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(content)
        return content

    @staticmethod
    def render(cutout: Image.Image, options: OutputOptions) -> bytes:
        """El lienzo como lo dibujaría el motor, reducido si es muy grande."""

        width, height = options.width, options.height
        scale = min(1.0, SAMPLE_MAX_SIDE / max(width, height))

        if scale < 1:
            width = max(MIN_SIDE, round(width * scale))
            height = max(MIN_SIDE, round(height * scale))

        margin = max(1, min(round(ENGINE_MARGIN * scale), (min(width, height) - 1) // 2))
        canvas = normalize_image(
            cutout,
            OutputOptions(width=width, height=height, background=options.background),
            margin=margin,
        )
        return _png(canvas)
