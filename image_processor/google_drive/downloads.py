"""
Descarga segura de imágenes de una carpeta de Drive (GD-02).

Solo lectura: usa files.list, files.get y files.get_media. Nunca
modifica Drive.

Nombres locales
---------------

    <nombre saneado>--<fileId codificado><ext>
    <nombre saneado>--<fileId codificado>.rev-<md5[:12]><ext>

- El fileId siempre forma parte del nombre: dos archivos de Drive con
  el mismo nombre nunca chocan.
- El fileId se codifica de forma reversible para sistemas que no
  distinguen mayúsculas: cada mayúscula se escribe como "~" seguido de
  la minúscula ("1AbC" -> "1~ab~c").
- El nombre remoto se sanea (NFC, sin separadores, caracteres
  prohibidos en Windows ni nombres reservados) y se recorta. Se
  colapsan "--" y "__" para no interferir con el separador ni con la
  política de nombres del motor local (H-04).
- ".rev-" se usa cuando el archivo de Drive cambió de contenido desde
  una descarga anterior: la copia anterior se conserva.

Procedencia
-----------

Cada imagen descargada tiene un registro en .drive/<nombre>.json con
el fileId exacto, el nombre original, los metadatos de Drive y el
SHA-256 local. Un archivo local nunca se atribuye a un fileId distinto
del de su registro, y un archivo sin registro nunca se sobrescribe.

Integridad
----------

Antes de descargar se exigen metadatos completos, canDownload, un tipo
admitido, un tamaño declarado dentro del límite y el md5Checksum de
Drive (sin checksum no se descarga). Se descarga por bloques a un
temporal; se comparan el tamaño, el MD5 (y el SHA-256 si Drive lo
informa); Pillow debe abrir y decodificar la imagen con sus
protecciones activas; y una nueva consulta confirma que el archivo no
cambió en Drive durante la descarga. Solo entonces se registra el
SHA-256 como pendiente, se publica la imagen sin sobrescribir y se
completa el registro.

No se admiten descargas concurrentes sobre la misma carpeta local.
"""

import errno
import hashlib
import json
import os
import re
import stat
import tempfile
import time
import unicodedata
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from googleapiclient.http import MediaIoBaseDownload
from PIL import Image

from ..atomic_write import STALE_TEMP_AGE_SECONDS, publish_file_exclusive
from ..naming import name_key
from . import client, queries
from .errors import (
    DriveAuthError,
    DriveError,
    DriveTransientError,
)


MIB = 1024 * 1024

DEFAULT_CHUNK_SIZE = 4 * MIB
DEFAULT_MAX_FILE_BYTES = 25 * MIB

# Reintentos de bloques por archivo (en total, no por bloque).
MAX_DOWNLOAD_RETRIES = 4

# Fallos de red seguidos que detienen el lote.
MAX_CONSECUTIVE_TRANSIENT_FAILURES = 3

# Respuestas exitosas seguidas sin datos nuevos que abortan una descarga.
MAX_STALLED_CHUNKS = 3

METADATA_DIR = ".drive"
RECORD_SCHEMA_VERSION = 1
GENERATOR = "drive-image-processor"

PENDING = "pending"
COMPLETED = "completed"

MAX_STEM_LENGTH = 80

# Formato que Pillow debe detectar para cada tipo MIME. Muchas
# cámaras y teléfonos guardan JPEG como MPO (JPEG con varias vistas).
EXPECTED_FORMATS = {
    "image/jpeg": {"JPEG", "MPO"},
    "image/png": {"PNG"},
    "image/webp": {"WEBP"},
}

# Extensiones que corresponden a cada tipo MIME.
EXTENSIONS_BY_MIME = {
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
}

FILE_FIELDS = (
    "id, name, mimeType, size, md5Checksum, sha256Checksum, version, "
    "headRevisionId, modifiedTime, trashed, capabilities(canDownload)"
)

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in "123456789¹²³"),
    *(f"LPT{i}" for i in "123456789¹²³"),
}
_IMAGE_TEMP = re.compile(r"\..+\.(?:jpg|jpeg|png|webp)\.[a-z0-9_]{8}\.tmp")
_RECORD_TEMP = re.compile(r"\..+\.json\.[a-z0-9_]{8}\.tmp")


# Nombres ---------------------------------------------------------------

def encode_file_id(file_id: str) -> str:
    """
    fileId -> texto que no depende de mayúsculas, de forma reversible.
    El fileId original no se modifica: se guarda tal cual en el registro.
    """
    queries.validate_file_id(file_id)

    return "".join(
        f"~{char.lower()}" if "A" <= char <= "Z" else char
        for char in file_id
    )


def decode_file_id(encoded: str) -> str:
    chars = iter(encoded)
    decoded = []

    for char in chars:
        decoded.append(next(chars).upper() if char == "~" else char)

    return "".join(decoded)


def safe_stem(name: str) -> str:
    """Nombre remoto -> nombre base seguro para Windows y Linux."""

    text = unicodedata.normalize("NFC", name)
    text = _FORBIDDEN.sub("_", text)

    stem, dot, _ = text.rpartition(".")
    if not dot:
        stem = text

    stem = re.sub(r"_{2,}", "_", stem)
    stem = re.sub(r"-{2,}", "-", stem)
    stem = stem.strip(" .-")

    if not stem:
        stem = "imagen"

    if stem.split(".")[0].upper() in _RESERVED:
        stem = f"_{stem}"

    stem = stem[:MAX_STEM_LENGTH].rstrip(" .-")

    return stem or "imagen"


def local_name(remote_name: str, file_id: str) -> str:
    """Nombre local determinista de una imagen de Drive."""

    extension = Path(_FORBIDDEN.sub("_", remote_name)).suffix.lower()
    return f"{safe_stem(remote_name)}--{encode_file_id(file_id)}{extension}"


def revision_name(name: str, md5_checksum: str) -> str:
    path = Path(name)
    return f"{path.stem}.rev-{md5_checksum[:12]}{path.suffix}"


# Resultado -------------------------------------------------------------

DOWNLOADED = "downloaded"
VERIFIED = "verified"
RECOVERED = "recovered"
REJECTED = "rejected"
FAILED = "failed"


@dataclass(frozen=True)
class DownloadItem:
    file_id: str | None
    name: str | None
    status: str
    local_name: str | None = None
    reason: str | None = None
    bytes: int = 0
    # False: el PNG se publicó, pero su registro quedó pendiente.
    registered: bool = True


@dataclass
class DownloadSummary:
    folder: queries.DriveFolder
    items: list[DownloadItem] = field(default_factory=list)
    temp_removed: int = 0
    aborted: DriveError | None = None

    def count(self, status: str) -> int:
        return sum(item.status == status for item in self.items)

    @property
    def bytes_downloaded(self) -> int:
        return sum(item.bytes for item in self.items if item.status == DOWNLOADED)

    @property
    def unregistered(self) -> int:
        return sum(not item.registered for item in self.items)


class _Rejected(Exception):
    """El archivo no cumple la política: no se descarga."""


class _Failed(Exception):
    """La descarga o la verificación falló: no se publica nada."""


class InvalidRecordError(_Failed):
    """Un registro de procedencia de .drive/ está dañado o no es válido."""


class _LocalWriteError(_Failed):
    """
    Error de E/S del disco local (no de la red): no se reintenta la
    descarga ni cuenta como fallo de red.
    """


def _local_error_message(exc: OSError) -> str:
    if exc.errno == errno.ENOSPC:
        detail = "no queda espacio en el disco local"
    elif isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM):
        detail = "permiso denegado al escribir en la carpeta local"
    else:
        detail = f"no se pudo escribir en la carpeta local ({type(exc).__name__})"

    return f"error local: {detail}."


# Registros de procedencia ---------------------------------------------

def _record_path(output_dir: Path, name: str) -> Path:
    return output_dir / METADATA_DIR / f"{name}.json"


_RECORD_KEYS = {
    "schema_version", "generator", "status", "file_id", "name",
    "mime_type", "size", "md5_checksum", "sha256", "drive_sha256",
    "version", "head_revision_id", "modified_time", "folder_id",
    "local_name", "recorded_at",
}


def read_record(output_dir: Path, name: str) -> dict | None:
    """
    Registro de procedencia de la imagen local name, o None si no
    tiene. Un registro dañado lanza InvalidRecordError.
    """
    path = _record_path(output_dir, name)

    if not path.exists():
        return None

    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        record = None

    valid = (
        isinstance(record, dict)
        and set(record) == _RECORD_KEYS
        and record["schema_version"] == RECORD_SCHEMA_VERSION
        and record["status"] in (PENDING, COMPLETED)
        and isinstance(record["file_id"], str)
        and isinstance(record["sha256"], str)
        and _HEX64.fullmatch(record["sha256"]) is not None
        and isinstance(record["md5_checksum"], str)
        and isinstance(record["size"], int)
        and record["local_name"] == name
    )

    if not valid:
        raise InvalidRecordError(
            f"el registro local {METADATA_DIR}/{name}.json está dañado; "
            "no se modifica ni se sobrescribe la imagen."
        )

    return record


def _write_record(output_dir: Path, name: str, record: dict) -> None:
    """Escritura atómica del registro (archivo propio de la aplicación)."""

    path = _record_path(output_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = (json.dumps(record, indent=2, ensure_ascii=False) + "\n").encode()

    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )

    try:
        with os.fdopen(fd, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())

        os.replace(tmp_name, path)

    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _remove_stale_temps(output_dir: Path) -> int:
    removed = 0
    now = time.time()

    for directory, pattern in (
        (output_dir, _IMAGE_TEMP),
        (output_dir / METADATA_DIR, _RECORD_TEMP),
    ):
        if not directory.is_dir():
            continue

        for path in directory.iterdir():
            if not pattern.fullmatch(path.name):
                continue

            try:
                info = path.lstat()

                if (
                    stat.S_ISREG(info.st_mode)
                    and now - info.st_mtime >= STALE_TEMP_AGE_SECONDS
                ):
                    os.unlink(path)
                    removed += 1
            except OSError:
                continue

    return removed


# Verificaciones ---------------------------------------------------------

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for block in iter(lambda: file.read(MIB), b""):
            digest.update(block)

    return digest.hexdigest()


def _parse_size(value) -> int | None:
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _check_metadata(meta: dict, file_id: str, max_bytes: int) -> dict:
    """Metadatos validados del archivo, o _Rejected/_Failed."""

    if not isinstance(meta, dict) or meta.get("id") != file_id:
        raise _Failed("Drive devolvió metadatos de otro archivo.")

    if meta.get("trashed"):
        raise _Rejected("está en la papelera")

    capabilities = meta.get("capabilities")

    if not isinstance(capabilities, dict) or "canDownload" not in capabilities:
        raise _Rejected("Drive no informó si se puede descargar")

    if capabilities["canDownload"] is not True:
        raise _Rejected("sin permiso de descarga")

    name, mime = meta.get("name"), meta.get("mimeType")

    if not isinstance(name, str) or not name:
        raise _Rejected("metadatos incompletos")

    extension = Path(name).suffix.lower()

    if (
        mime not in EXPECTED_FORMATS
        or extension not in queries.SUPPORTED_EXTENSIONS
    ):
        raise _Rejected("formato no admitido")

    if extension not in EXTENSIONS_BY_MIME[mime]:
        raise _Rejected(
            f"la extensión {extension} no corresponde al tipo {mime}"
        )

    size = _parse_size(meta.get("size"))

    if size is None or size <= 0:
        raise _Rejected("tamaño no declarado")

    if size > max_bytes:
        raise _Rejected(
            f"supera el límite de {max_bytes / MIB:g} MiB ({size} bytes)"
        )

    md5 = meta.get("md5Checksum")

    if not isinstance(md5, str) or _HEX32.fullmatch(md5) is None:
        raise _Rejected(
            "sin checksum MD5 de Drive: no se puede verificar su integridad"
        )

    drive_sha256 = meta.get("sha256Checksum")

    if not isinstance(drive_sha256, str) or _HEX64.fullmatch(drive_sha256) is None:
        drive_sha256 = None

    return {
        "name": name,
        "mime_type": mime,
        "size": size,
        "md5_checksum": md5,
        "drive_sha256": drive_sha256,
        "version": meta.get("version"),
        "head_revision_id": meta.get("headRevisionId"),
        "modified_time": meta.get("modifiedTime"),
    }


class _HashingWriter:
    """Escribe a disco, calcula MD5 y SHA-256 y corta si hay exceso."""

    def __init__(self, file, limit: int):
        self.file = file
        self.limit = limit
        self.size = 0
        self.md5 = hashlib.md5(usedforsecurity=False)
        self.sha256 = hashlib.sha256()

    def write(self, data: bytes) -> int:
        self.size += len(data)

        if self.size > self.limit:
            raise _Failed(
                "Drive envió más bytes de los declarados; se descartó la "
                "descarga."
            )

        self.md5.update(data)
        self.sha256.update(data)

        try:
            return self.file.write(data)
        except OSError as exc:
            # Disco lleno o permisos: no es un error de la red, aunque
            # ocurra dentro de next_chunk().
            raise _LocalWriteError(_local_error_message(exc)) from None


def _verify_image(path: Path, mime: str) -> None:
    """Pillow debe abrir y decodificar la imagen con sus protecciones."""

    try:
        with warnings.catch_warnings():
            # Por encima de Image.MAX_IMAGE_PIXELS Pillow solo avisa;
            # aquí el aviso también rechaza la imagen.
            warnings.simplefilter("error", Image.DecompressionBombWarning)

            with Image.open(path) as image:
                image_format = image.format
                image.verify()

            with Image.open(path) as image:
                image.load()

    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise _Rejected(
            "imagen desproporcionadamente grande (protección de Pillow "
            "contra bombas de descompresión)"
        ) from None
    except Exception:
        raise _Failed(
            "el archivo descargado no es una imagen válida o está dañado."
        ) from None

    if image_format not in EXPECTED_FORMATS[mime]:
        raise _Failed(
            f"el contenido ({image_format}) no coincide con el tipo "
            f"declarado ({mime})."
        )


# Descarga ---------------------------------------------------------------

def _download_to_temp(
    service, file_id, info, output_dir, target, chunk_size, sleep,
    downloader_factory,
):
    """Descarga por bloques a un temporal verificado. Devuelve su ruta y hashes."""

    fd, tmp_name = tempfile.mkstemp(
        dir=output_dir, prefix=f".{target}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)

    try:
        with os.fdopen(fd, "wb") as file:
            writer = _HashingWriter(file, info["size"])
            request = service.files().get_media(
                fileId=file_id, supportsAllDrives=True
            )
            downloader = downloader_factory(writer, request, chunksize=chunk_size)

            retries = 0
            stalled = 0
            chunks = 0
            # Cota de respuestas exitosas: el doble de los bloques
            # esperados, para que ninguna respuesta anómala (por
            # ejemplo, un byte por bloque) prolongue la descarga sin fin.
            max_chunks = 2 * -(-info["size"] // chunk_size) + MAX_STALLED_CHUNKS
            done = False

            while not done:
                received = writer.size

                try:
                    _, done = downloader.next_chunk(num_retries=0)
                except _Failed:
                    raise
                except Exception as exc:
                    translated = client.translate_error(exc)

                    if translated is None:
                        raise

                    if not translated.retryable or retries >= MAX_DOWNLOAD_RETRIES:
                        raise translated.error from None

                    retries += 1
                    sleep(translated.retry_after or client.backoff(retries))
                    continue

                if done:
                    break

                chunks += 1

                if writer.size == received:
                    stalled += 1

                    if stalled >= MAX_STALLED_CHUNKS:
                        raise _Failed(
                            "Drive respondió sin progreso en la descarga; se "
                            "abortó y se descartó el temporal."
                        )
                else:
                    stalled = 0

                if chunks >= max_chunks:
                    raise _Failed(
                        "Drive envió demasiados bloques para el tamaño "
                        "declarado; se abortó y se descartó el temporal."
                    )

            try:
                file.flush()
                os.fsync(file.fileno())
            except OSError as exc:
                raise _LocalWriteError(_local_error_message(exc)) from None

        if writer.size != info["size"]:
            raise _Failed(
                f"descarga incompleta: se recibieron {writer.size} de "
                f"{info['size']} bytes."
            )

        if writer.md5.hexdigest() != info["md5_checksum"]:
            raise _Failed("el MD5 descargado no coincide con el de Drive.")

        sha256 = writer.sha256.hexdigest()

        if info["drive_sha256"] is not None and sha256 != info["drive_sha256"]:
            raise _Failed("el SHA-256 descargado no coincide con el de Drive.")

        _verify_image(tmp, info["mime_type"])

        return tmp, sha256

    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _record(file_id, folder_id, info, sha256, name, status):
    return {
        "schema_version": RECORD_SCHEMA_VERSION,
        "generator": GENERATOR,
        "status": status,
        "file_id": file_id,
        "name": info["name"],
        "mime_type": info["mime_type"],
        "size": info["size"],
        "md5_checksum": info["md5_checksum"],
        "sha256": sha256,
        "drive_sha256": info["drive_sha256"],
        "version": info["version"],
        "head_revision_id": info["head_revision_id"],
        "modified_time": info["modified_time"],
        "folder_id": folder_id,
        "local_name": name,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _process_image(
    service, image, folder_id, output_dir, exists, *, max_file_bytes,
    chunk_size, sleep, execute, downloader_factory,
):
    file_id = image.id

    try:
        queries.validate_file_id(file_id)
    except ValueError:
        raise _Failed("Drive devolvió un identificador inesperado.") from None

    meta = execute(service.files().get(
        fileId=file_id, fields=FILE_FIELDS, supportsAllDrives=True
    ))
    info = _check_metadata(meta, file_id, max_file_bytes)

    base = local_name(info["name"], file_id)
    revision = revision_name(base, info["md5_checksum"])

    # 1. ¿Este contenido ya está descargado (o pendiente de registro)?
    for name in (base, revision):
        record = read_record(output_dir, name)

        if record is None:
            continue

        if record["file_id"] != file_id:
            raise _Failed(
                f"el registro local de {name} corresponde a otro archivo de "
                "Drive; no se modifica."
            )

        if (record["md5_checksum"], record["size"]) != (
            info["md5_checksum"], info["size"],
        ):
            continue

        if not exists(name):
            # Registrado pero ausente: se descarga de nuevo con ese nombre.
            target, previous = name, record
            break

        if _sha256(output_dir / name) != record["sha256"]:
            raise _Failed(
                f"{name} no coincide con su registro (modificado o "
                "incompleto); no se sobrescribe."
            )

        if record["status"] == COMPLETED:
            return DownloadItem(file_id, info["name"], VERIFIED, name)

        record = dict(record, status=COMPLETED)
        _write_record(output_dir, name, record)
        return DownloadItem(
            file_id, info["name"], RECOVERED, name,
            reason="se completó un registro interrumpido",
        )

    else:
        # 2. Contenido nuevo: nombre base o, si está ocupado, revisión.
        previous = None
        base_record = read_record(output_dir, base)

        if base_record is None:
            if exists(base):
                raise _Failed(
                    f"ya existe {base} sin registro de procedencia; no se "
                    "sobrescribe ni se atribuye a este archivo."
                )
            target = base
        else:
            if exists(revision):
                raise _Failed(
                    f"ya existe {revision} sin registro de procedencia; no "
                    "se sobrescribe."
                )
            target = revision

    tmp, sha256 = _download_to_temp(
        service, file_id, info, output_dir, target, chunk_size, sleep,
        downloader_factory,
    )

    try:
        # 3. ¿Cambió el archivo en Drive durante la descarga?
        final = execute(service.files().get(
            fileId=file_id,
            fields="id, size, md5Checksum, headRevisionId, trashed",
            supportsAllDrives=True,
        ))

        if (
            final.get("id") != file_id
            or final.get("trashed")
            or _parse_size(final.get("size")) != info["size"]
            or final.get("md5Checksum") != info["md5_checksum"]
            or final.get("headRevisionId") != info["head_revision_id"]
        ):
            raise _Failed(
                "el archivo cambió en Drive durante la descarga; vuelva a "
                "ejecutar el comando."
            )

        # 4. Evidencia, publicación exclusiva y registro completo.
        _write_record(
            output_dir, target,
            _record(file_id, folder_id, info, sha256, target, PENDING),
        )

        try:
            publish_file_exclusive(tmp, output_dir / target)
        except FileExistsError:
            if previous is None:
                _record_path(output_dir, target).unlink(missing_ok=True)
            else:
                _write_record(output_dir, target, previous)
            raise _Failed(
                f"apareció {target} durante la descarga; no se sobrescribe."
            ) from None

    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    registered = True

    try:
        _write_record(
            output_dir, target,
            _record(file_id, folder_id, info, sha256, target, COMPLETED),
        )
    except OSError:
        registered = False

    reason = None
    if target == revision:
        reason = "el archivo cambió en Drive; se conservó la copia anterior"
    if not registered:
        reason = "registro pendiente: la próxima ejecución lo completará"

    return DownloadItem(
        file_id, info["name"], DOWNLOADED, target,
        reason=reason, bytes=info["size"], registered=registered,
    )


def download_folder(
    service,
    folder_id: str,
    output_dir: Path,
    *,
    limit: int | None = None,
    originals_only: bool = False,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    sleep=time.sleep,
    execute=None,
    downloader_factory=MediaIoBaseDownload,
    on_item=None,
) -> DownloadSummary:
    """
    Descarga las imágenes admitidas de una carpeta (sin subcarpetas).

    limit cuenta las imágenes elegibles (descargadas o ya verificadas),
    en orden por nombre y fileId. Los rechazos del listado se informan
    siempre.
    """
    if limit is not None and limit < 1:
        raise ValueError("El límite debe ser al menos 1.")

    if max_file_bytes < 1 or chunk_size < 1:
        raise ValueError("El tamaño máximo y el de bloque deben ser positivos.")

    execute = execute or client.execute
    output_dir = Path(output_dir)

    listing = queries.list_images(
        service, folder_id, originals_only=originals_only, execute=execute
    )

    output_dir.mkdir(parents=True, exist_ok=True)

    summary = DownloadSummary(folder=listing.folder)
    summary.temp_removed = _remove_stale_temps(output_dir)

    existing = {name_key(path.name) for path in output_dir.iterdir()}

    def exists(name: str) -> bool:
        return name_key(name) in existing or (output_dir / name).exists()

    def report(item):
        summary.items.append(item)
        if on_item is not None:
            on_item(item)

    for skipped in listing.skipped:
        report(DownloadItem(skipped.id, skipped.name, REJECTED, reason=skipped.reason))

    images = sorted(
        listing.images,
        key=lambda image: (
            unicodedata.normalize("NFC", image.name).casefold(),
            image.name,
            image.id,
        ),
    )

    if limit is not None:
        images = images[:limit]

    consecutive_transient = 0

    for image in images:
        try:
            item = _process_image(
                service, image, listing.folder.id, output_dir, exists,
                max_file_bytes=max_file_bytes, chunk_size=chunk_size,
                sleep=sleep, execute=execute,
                downloader_factory=downloader_factory,
            )
        except _Rejected as exc:
            item = DownloadItem(image.id, image.name, REJECTED, reason=str(exc))
        except _Failed as exc:
            item = DownloadItem(image.id, image.name, FAILED, reason=str(exc))
        except OSError as exc:
            # Errores locales (disco lleno, permisos): sin publicar nada.
            item = DownloadItem(
                image.id, image.name, FAILED, reason=_local_error_message(exc)
            )
        except DriveAuthError as exc:
            summary.aborted = exc
            break
        except DriveError as exc:
            item = DownloadItem(image.id, image.name, FAILED, reason=str(exc))

            if isinstance(exc, DriveTransientError):
                consecutive_transient += 1
            else:
                consecutive_transient = 0

            report(item)

            if consecutive_transient >= MAX_CONSECUTIVE_TRANSIENT_FAILURES:
                summary.aborted = DriveTransientError(
                    f"Se detuvo la descarga después de "
                    f"{consecutive_transient} fallos de red seguidos."
                )
                break
            continue

        consecutive_transient = 0
        report(item)

    return summary
