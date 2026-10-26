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

Renombres
---------

Si el original solo cambió de nombre en Drive (mismo fileId, tamaño y
MD5), se reutiliza la copia local ya registrada cuyo SHA-256 sigue
coincidiendo con su registro: no se descarga de nuevo. La versión y la
fecha de modificación no se exigen (son metadatos). La copia y su
registro conservan el nombre anterior.

Disco local lleno
-----------------

La falta de espacio (ENOSPC, EDQUOT o, en Windows, ERROR_DISK_FULL,
ERROR_HANDLE_DISK_FULL y ERROR_DISK_QUOTA_EXCEEDED) se reconoce en cada
escritura y se informa con su fase. Con stop_on_disk_full, detiene el
lote (LocalDiskFullError); sin él, la imagen falla como cualquier otro
error local, como siempre.

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
    DriveQuotaError,
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
# Solo en la vista previa: se descargaría.
PLANNED = "planned"

# Fases de escritura local (para informar una falta de espacio).
PHASE_TEMP = "escritura del temporal"
PHASE_PENDING_RECORD = "registro pendiente"
PHASE_COMPLETED_RECORD = "registro completo"
PHASE_LOCAL = "escritura local"

# Falta de espacio: POSIX y, en Windows, sus códigos propios. Python
# traduce ERROR_HANDLE_DISK_FULL (39) y ERROR_DISK_QUOTA_EXCEEDED (1295)
# a EINVAL, así que errno no alcanza.
_DISK_FULL_ERRNOS = frozenset(
    code for code in (errno.ENOSPC, getattr(errno, "EDQUOT", None)) if code is not None
)
_DISK_FULL_WINERRORS = frozenset({39, 112, 1295})


def is_disk_full(exc: BaseException) -> bool:
    """¿El error local es una falta de espacio en el disco?"""

    return isinstance(exc, OSError) and (
        exc.errno in _DISK_FULL_ERRNOS
        or getattr(exc, "winerror", None) in _DISK_FULL_WINERRORS
    )


class LocalDiskFullError(RuntimeError):
    """
    No queda espacio en el disco local: detiene el lote. Conserva la
    imagen y la fase afectadas.
    """

    def __init__(self, file_id, name, phase):
        self.file_id = file_id
        self.name = name
        self.phase = phase
        super().__init__(
            f"No queda espacio en el disco local ({phase}) al descargar "
            f"{name} ({file_id}). Se detuvo la descarga; libere espacio y "
            "vuelva a ejecutar el comando."
        )


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
    # Fase en la que faltó espacio en el disco local, si ocurrió.
    disk_full_phase: str | None = None


@dataclass
class DownloadSummary:
    folder: queries.DriveFolder
    items: list[DownloadItem] = field(default_factory=list)
    temp_removed: int = 0
    aborted: DriveError | LocalDiskFullError | None = None

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

    def __init__(self, exc: OSError, phase: str):
        super().__init__(_local_error_message(exc))
        self.phase = phase
        self.disk_full = is_disk_full(exc)


def _local_error_message(exc: OSError) -> str:
    if is_disk_full(exc):
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
            raise _LocalWriteError(exc, PHASE_TEMP) from None


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

    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=output_dir, prefix=f".{target}.", suffix=".tmp"
        )
    except OSError as exc:
        raise _LocalWriteError(exc, PHASE_TEMP) from None

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
                raise _LocalWriteError(exc, PHASE_TEMP) from None

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


@dataclass(frozen=True)
class _Plan:
    """
    Qué hacer con una imagen: "verified" (ya está), "recover" (completar
    un registro interrumpido) o "download" (descargar en name).
    """

    action: str
    info: dict
    name: str
    previous: dict | None = None
    revision: bool = False
    reason: str | None = None


def _check_listing(image, info) -> None:
    """
    El contenido no cambió desde el listado (sync): si cambió, no se
    descarga una revisión distinta de la seleccionada.
    """
    changed = (
        image.md5_checksum is not None and image.md5_checksum != info["md5_checksum"]
    ) or (image.size is not None and image.size != info["size"])

    if changed:
        raise _Failed(
            "el contenido del original cambió en Drive después del listado; "
            "no se descarga una revisión distinta de la seleccionada. Vuelva "
            "a ejecutar la sincronización."
        )


def _records_by_file_id(output_dir: Path) -> dict[str, list[tuple[str, dict]]]:
    """Registros válidos de la carpeta, por fileId (para los renombres)."""

    index: dict[str, list[tuple[str, dict]]] = {}
    directory = output_dir / METADATA_DIR

    if not directory.is_dir():
        return index

    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if not path.name.endswith(".json") or not path.is_file():
            continue

        name = path.name[: -len(".json")]

        try:
            record = read_record(output_dir, name)
        except InvalidRecordError:
            # Se informa al consultarlo por su nombre; aquí no se usa.
            continue

        if record is not None:
            index.setdefault(record["file_id"], []).append((name, record))

    return index


def _same_content_copy(output_dir, file_id, info, exists, records, exclude):
    """
    Copia local ya registrada del mismo fileId y del mismo contenido
    (tamaño, MD5 y, si Drive lo informa, SHA-256), cuyo archivo sigue
    coincidiendo con su SHA-256. Se prefiere un registro completo y,
    entre varios, el de nombre menor: la elección es determinista.
    """
    candidates = []

    for name, record in records().get(file_id, []):
        if name in exclude:
            continue

        same_content = (
            (record["md5_checksum"], record["size"]) == (info["md5_checksum"], info["size"])
            and info["drive_sha256"] in (None, record["sha256"])
        )
        path = output_dir / name

        if not same_content or not exists(name) or path.is_symlink() or not path.is_file():
            continue

        if _sha256(path) != record["sha256"]:
            continue

        candidates.append((record["status"] != COMPLETED, name, record))

    return min(candidates, default=None)


def _plan_image(
    service, image, output_dir, exists, records, *, max_file_bytes, execute,
    check_listing,
) -> _Plan:
    """Decisión sobre una imagen, sin escribir nada."""

    file_id = image.id

    try:
        queries.validate_file_id(file_id)
    except ValueError:
        raise _Failed("Drive devolvió un identificador inesperado.") from None

    meta = execute(service.files().get(
        fileId=file_id, fields=FILE_FIELDS, supportsAllDrives=True
    ))
    info = _check_metadata(meta, file_id, max_file_bytes)

    if check_listing:
        _check_listing(image, info)

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
            return _Plan("download", info, name, previous=record, revision=name == revision)

        if _sha256(output_dir / name) != record["sha256"]:
            raise _Failed(
                f"{name} no coincide con su registro (modificado o "
                "incompleto); no se sobrescribe."
            )

        if record["status"] == COMPLETED:
            return _Plan("verified", info, name)

        return _Plan("recover", info, name)

    # 2. ¿Solo cambió el nombre? Mismo contenido con otro nombre local.
    copy = _same_content_copy(
        output_dir, file_id, info, exists, records, exclude={base, revision}
    )

    if copy is not None:
        pending, name, record = copy
        reason = (
            f"el original se renombró en Drive ({record['name']} -> "
            f"{info['name']}); se reutiliza la copia local {name}, con el "
            "mismo contenido."
        )
        return _Plan("recover" if pending else "verified", info, name, reason=reason)

    # 3. Contenido nuevo: nombre base o, si está ocupado, revisión.
    base_record = read_record(output_dir, base)

    if base_record is None:
        if exists(base):
            raise _Failed(
                f"ya existe {base} sin registro de procedencia; no se "
                "sobrescribe ni se atribuye a este archivo."
            )
        return _Plan("download", info, base)

    if exists(revision):
        raise _Failed(
            f"ya existe {revision} sin registro de procedencia; no "
            "se sobrescribe."
        )

    return _Plan("download", info, revision, revision=True)


def _preview_item(file_id, plan: _Plan) -> DownloadItem:
    """Resultado que tendría el plan (vista previa: nada se escribe)."""

    info = plan.info

    if plan.action == "verified":
        return DownloadItem(file_id, info["name"], VERIFIED, plan.name, reason=plan.reason)

    if plan.action == "recover":
        return DownloadItem(
            file_id, info["name"], RECOVERED, plan.name,
            reason=plan.reason or "se completaría un registro interrumpido",
        )

    reason = "se descargaría"
    if plan.revision:
        reason = "se descargaría como revisión nueva; se conservaría la copia anterior"

    return DownloadItem(file_id, info["name"], PLANNED, plan.name, reason=reason)


def _process_image(
    service, image, folder_id, output_dir, exists, records, *, max_file_bytes,
    chunk_size, sleep, execute, downloader_factory, check_listing,
):
    file_id = image.id
    plan = _plan_image(
        service, image, output_dir, exists, records,
        max_file_bytes=max_file_bytes, execute=execute,
        check_listing=check_listing,
    )
    info = plan.info

    if plan.action == "verified":
        return DownloadItem(file_id, info["name"], VERIFIED, plan.name, reason=plan.reason)

    if plan.action == "recover":
        record = dict(read_record(output_dir, plan.name), status=COMPLETED)
        _write_record(output_dir, plan.name, record)
        return DownloadItem(
            file_id, info["name"], RECOVERED, plan.name,
            reason=plan.reason or "se completó un registro interrumpido",
        )

    target, previous = plan.name, plan.previous

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
        try:
            _write_record(
                output_dir, target,
                _record(file_id, folder_id, info, sha256, target, PENDING),
            )
        except OSError as exc:
            raise _LocalWriteError(exc, PHASE_PENDING_RECORD) from None

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
    disk_full_phase = None

    try:
        _write_record(
            output_dir, target,
            _record(file_id, folder_id, info, sha256, target, COMPLETED),
        )
    except OSError as exc:
        # La imagen ya está publicada y su registro pendiente tiene el
        # SHA-256: la próxima ejecución la recupera sin descargarla.
        registered = False
        if is_disk_full(exc):
            disk_full_phase = PHASE_COMPLETED_RECORD

    reason = None
    if plan.revision:
        reason = "el archivo cambió en Drive; se conservó la copia anterior"
    if not registered:
        reason = "registro pendiente: la próxima ejecución lo completará"

    return DownloadItem(
        file_id, info["name"], DOWNLOADED, target,
        reason=reason, bytes=info["size"], registered=registered,
        disk_full_phase=disk_full_phase,
    )


def select_images(listing: queries.ImageListing, limit: int | None = None):
    """Imágenes del listado en el orden de las descargas, hasta limit."""

    images = sorted(
        listing.images,
        key=lambda image: (
            unicodedata.normalize("NFC", image.name).casefold(),
            image.name,
            image.id,
        ),
    )

    return images[:limit] if limit is not None else images


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
    listing: queries.ImageListing | None = None,
    file_ids=None,
    dry_run: bool = False,
    stop_on_disk_full: bool = False,
    stop_on_errors: tuple = (),
) -> DownloadSummary:
    """
    Descarga las imágenes admitidas de una carpeta (sin subcarpetas).

    limit cuenta las imágenes elegibles (descargadas o ya verificadas),
    en orden por nombre y fileId. Los rechazos del listado se informan
    siempre.

    Para sync (GD-04):

    - listing reutiliza un listado ya hecho (no se vuelve a listar) y
      exige que el contenido de cada imagen (MD5 y tamaño) no haya
      cambiado desde entonces;
    - file_ids limita la descarga a esas imágenes, en el mismo orden
      (no se combina con limit);
    - dry_run decide qué se haría sin escribir nada (estado PLANNED para
      lo que se descargaría);
    - stop_on_disk_full detiene el lote si falta espacio en el disco
      (aborted: LocalDiskFullError);
    - stop_on_errors son errores de Drive que detienen el lote además
      de los habituales (por ejemplo, la conexión segura fallida); la
      imagen afectada se informa como fallida.
    """
    if limit is not None and limit < 1:
        raise ValueError("El límite debe ser al menos 1.")

    if limit is not None and file_ids is not None:
        raise ValueError("limit y file_ids no se combinan.")

    if max_file_bytes < 1 or chunk_size < 1:
        raise ValueError("El tamaño máximo y el de bloque deben ser positivos.")

    execute = execute or client.execute
    output_dir = Path(output_dir)
    check_listing = listing is not None

    if listing is None:
        listing = queries.list_images(
            service, folder_id, originals_only=originals_only, execute=execute
        )

    summary = DownloadSummary(folder=listing.folder)

    if dry_run:
        # Vista previa: ni carpetas ni limpieza de temporales.
        existing = (
            {name_key(path.name) for path in output_dir.iterdir()}
            if output_dir.is_dir()
            else set()
        )
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
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

    if file_ids is None:
        images = select_images(listing, limit)
    else:
        wanted = set(file_ids)
        images = [image for image in select_images(listing) if image.id in wanted]

    # Índice de registros por fileId, solo si hace falta (renombres).
    index = {}

    def records():
        if "value" not in index:
            index["value"] = _records_by_file_id(output_dir)
        return index["value"]

    consecutive_transient = 0

    for image in images:
        try:
            if dry_run:
                item = _preview_item(image.id, _plan_image(
                    service, image, output_dir, exists, records,
                    max_file_bytes=max_file_bytes, execute=execute,
                    check_listing=check_listing,
                ))
            else:
                item = _process_image(
                    service, image, listing.folder.id, output_dir, exists, records,
                    max_file_bytes=max_file_bytes, chunk_size=chunk_size,
                    sleep=sleep, execute=execute,
                    downloader_factory=downloader_factory,
                    check_listing=check_listing,
                )
        except _Rejected as exc:
            item = DownloadItem(image.id, image.name, REJECTED, reason=str(exc))
        except _LocalWriteError as exc:
            item = DownloadItem(
                image.id, image.name, FAILED, reason=str(exc),
                disk_full_phase=exc.phase if exc.disk_full else None,
            )
        except _Failed as exc:
            item = DownloadItem(image.id, image.name, FAILED, reason=str(exc))
        except OSError as exc:
            # Errores locales (disco lleno, permisos): sin publicar nada.
            item = DownloadItem(
                image.id, image.name, FAILED, reason=_local_error_message(exc),
                disk_full_phase=PHASE_LOCAL if is_disk_full(exc) else None,
            )
        except (DriveAuthError, DriveQuotaError) as exc:
            # Autorización o cuota agotada: afectan a todo el lote. No es
            # un fallo de la imagen ni de la red; nada quedó publicado.
            summary.aborted = exc
            break
        except DriveError as exc:
            item = DownloadItem(image.id, image.name, FAILED, reason=str(exc))

            if stop_on_errors and isinstance(exc, stop_on_errors):
                report(item)
                summary.aborted = exc
                break

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

        if stop_on_disk_full and item.disk_full_phase is not None:
            summary.aborted = LocalDiskFullError(
                item.file_id, item.name, item.disk_full_phase
            )
            break

    return summary
