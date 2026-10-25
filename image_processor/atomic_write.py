"""
Publicación atómica de archivos sin sobrescrituras.

El resultado se escribe primero en un temporal de la misma
carpeta y solo se publica cuando está completo. Si el destino
ya existe, la publicación falla con FileExistsError.

No se admiten lotes concurrentes sobre la misma carpeta.
"""

import os
import re
import stat
import tempfile
import time
from pathlib import Path
from time import sleep

from PIL import Image


TEMP_SUFFIX = ".tmp"

# Esperas entre reintentos cuando un antivirus o un
# sincronizador retiene el temporal recién escrito.
PUBLISH_RETRY_DELAYS = (0.1, 0.2, 0.4)

# Un temporal más reciente podría pertenecer a un proceso activo.
STALE_TEMP_AGE_SECONDS = 24 * 60 * 60


def _silent_unlink(path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _rename_no_replace(tmp: Path, final: Path) -> None:
    # En Windows, rename() falla si el destino existe.
    os.rename(tmp, final)


def _link_no_replace(tmp: Path, final: Path) -> None:
    # link() falla si el destino existe; rename()
    # lo sobrescribiría en POSIX.
    os.link(tmp, final)
    _silent_unlink(tmp)


_publish_once = (
    _rename_no_replace if os.name == "nt" else _link_no_replace
)


def _publish_no_overwrite(tmp: Path, final: Path) -> None:
    for delay in PUBLISH_RETRY_DELAYS:
        try:
            _publish_once(tmp, final)
            return
        except PermissionError:
            # FileExistsError no es PermissionError:
            # un destino existente nunca se reintenta.
            sleep(delay)

    _publish_once(tmp, final)


def publish_file_exclusive(tmp: Path, final: Path) -> None:
    """
    Publica un temporal ya escrito y verificado (en la misma carpeta)
    con el nombre final, sin sobrescribir: si el destino existe, lanza
    FileExistsError y el temporal queda para que lo elimine quien lo
    creó.
    """
    _publish_no_overwrite(Path(tmp), Path(final))


def save_png_exclusive(
    image: Image.Image,
    final_path: Path,
) -> None:
    """
    Guarda la imagen como PNG sin sobrescribir.

    El destino aparece completo o no aparece. Los permisos
    del archivo son los restrictivos de mkstemp.
    """
    final_path = Path(final_path)

    if final_path.exists():
        raise FileExistsError(
            f"El destino ya existe: {final_path}"
        )

    fd, tmp_name = tempfile.mkstemp(
        dir=final_path.parent,
        prefix=f"{final_path.name}.",
        suffix=TEMP_SUFFIX,
    )
    tmp_path = Path(tmp_name)

    try:
        with os.fdopen(fd, "wb") as target:
            image.save(target, format="PNG")
            target.flush()
            os.fsync(target.fileno())

        _publish_no_overwrite(tmp_path, final_path)

    except BaseException:
        # Incluye KeyboardInterrupt.
        _silent_unlink(tmp_path)
        raise


def remove_stale_temp_files(
    directory: Path,
    prefix: str,
    max_age_seconds: float = STALE_TEMP_AGE_SECONDS,
) -> int:
    """
    Elimina temporales abandonados por ejecuciones anteriores.

    Solo considera archivos regulares que coinciden exactamente
    con el formato de save_png_exclusive y que superan la
    antigüedad indicada. Los archivos en uso se conservan.
    """
    pattern = re.compile(
        re.escape(prefix)
        + r".+\.png\.[a-z0-9_]{8}"
        + re.escape(TEMP_SUFFIX)
    )

    now = time.time()
    removed = 0

    for path in Path(directory).iterdir():
        if not pattern.fullmatch(path.name):
            continue

        try:
            info = path.lstat()
        except OSError:
            continue

        if not stat.S_ISREG(info.st_mode):
            continue

        if now - info.st_mtime < max_age_seconds:
            continue

        try:
            os.unlink(path)
        except OSError:
            # En uso o eliminado por otro proceso.
            continue

        removed += 1

    return removed
