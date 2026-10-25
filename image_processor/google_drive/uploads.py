"""
Subida segura de los PNG verificados por GD-03A (GD-03B).

Solo crea archivos nuevos (files.create): nunca actualiza, mueve ni
elimina archivos de Drive, y nunca toca los originales.

Credenciales
------------

- Lectura (drive.readonly): la carpeta de destino, el original de cada
  resultado y los nombres que ya existen (también los ajenos, que
  drive.file no ve).
- Escritura (drive.file): comprobar el acceso a la carpeta de
  destino, preasignar IDs, crear los PNG y consultar los PNG propios.

drive.file solo permite crear archivos en carpetas que la aplicación
creó o que el usuario le autorizó (por ejemplo, con Google Picker). Si
la carpeta de destino no es accesible, la subida se detiene antes de
subir nada (DriveWriteAccessError); nunca se pide el permiso "drive".

Qué se sube
-----------

Solo PNG registrados como completos en el manifiesto de GD-03A, de un
original de Drive de la carpeta indicada y con la configuración
indicada (modelo, dimensiones y fondo), cuyo SHA-256 coincide con el
registrado y que Pillow reconoce como el PNG esperado. Se rechazan los
PNG sin registro, los alterados, los enlaces simbólicos y los
resultados de otras carpetas. Se suben los bytes verificados en
memoria: nunca una segunda lectura del disco.

Idempotencia
------------

Cada subida tiene una identidad (upload_key): el fileId y el SHA-256
del original, el SHA-256 del PNG, el modelo, las opciones, la versión
del procesamiento y la carpeta de destino. Se guarda en las
appProperties del PNG remoto y en un registro local:

    <resultados>/.uploads/<carpeta de destino codificada>/<png>.json

1. Si hay registro, se comprueba el PNG remoto registrado.
2. Si no, se busca en la carpeta de destino un PNG con la misma
   identidad (todas las páginas): si existe y se verifica, no se sube.
3. Si no existe, se preasigna un ID (files.generateIds), se registra
   como pendiente y recién entonces se crea el archivo con ese ID.
   Los reintentos usan el mismo ID: si un intento anterior llegó a
   Drive, este responde 409 y nunca se duplica.
4. Si la respuesta se pierde y no se puede confirmar, la subida queda
   incierta: la próxima ejecución consulta el ID reservado.

Nombres
-------

TRANSFORMADA_<nombre del original>[variante].png. Drive admite nombres
repetidos, así que el nombre no identifica: si ya existe en el destino
(un archivo ajeno u otro resultado), se usa el nombre de revisión
determinista __rev-<12 hex> (H-05); si también existe, se rechaza.
Ningún archivo existente se sobrescribe.

Verificación
------------

Después de crear cada PNG se consulta de nuevo: ID, nombre, carpeta,
mimeType image/png, tamaño, MD5 (y SHA-256 si Drive lo informa) y la
identidad. Una subida que no se puede verificar nunca se da por
completada.

No se admiten subidas concurrentes sobre la misma carpeta de resultados.
"""

import hashlib
import io
import json
import os
import re
import tempfile
import time
import unicodedata
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from PIL import Image

from ..manifest import COMPLETED, DRIVE_KIND, Manifest, ManifestEntry
from ..naming import OUTPUT_PREFIX, expected_format, name_key, output_name, with_revision
from ..pipeline import PIPELINE_VERSION
from ..processing import OutputOptions
from . import client, downloads, queries
from .auth import AUTH_WRITE_COMMAND
from .errors import (
    DriveAccessError,
    DriveAuthError,
    DriveCertificateError,
    DriveError,
    DriveNotFoundError,
    DriveResponseError,
    DriveTransientError,
    DriveWriteAccessError,
)


MIB = 1024 * 1024
PNG_MIME = "image/png"
DEFAULT_MODEL = "isnet-general-use"

# Múltiplo de 256 KiB, como exige la subida reanudable.
DEFAULT_CHUNK_SIZE = 4 * MIB

# Reintentos de una subida (en total, siempre con el mismo ID).
MAX_UPLOAD_RETRIES = 4

# Fallos de red seguidos que detienen el lote.
MAX_CONSECUTIVE_TRANSIENT_FAILURES = 3

PAGE_SIZE = 1000

LEDGER_DIR = ".uploads"
LEDGER_SCHEMA_VERSION = 1
GENERATOR = "drive-image-processor"

LEDGER_PENDING = "pending"
LEDGER_COMPLETED = "completed"
LEDGER_MISMATCH = "mismatch"

PROP_UPLOAD_KEY = "dip_upload_key"
PROP_SOURCE_FILE_ID = "dip_source_file_id"
PROP_SOURCE_SHA256 = "dip_source_sha256"
PROP_OUTPUT_SHA256 = "dip_output_sha256"
PROP_CONFIG = "dip_config"

REMOTE_FIELDS = (
    "id, name, mimeType, parents, size, md5Checksum, sha256Checksum, "
    "trashed, appProperties"
)

_ORIGINAL_FIELDS = "id, parents, md5Checksum, trashed"

_HEX32 = re.compile(r"[0-9a-f]{32}")
_VERSION = re.compile(r"[0-9]+")

_LEDGER_KEYS = {
    "schema_version", "generator", "status", "upload_key", "remote_id",
    "remote_name", "target_folder_id", "source_file_id", "source_folder_id",
    "output_name", "output_sha256", "size", "md5_checksum", "recorded_at",
    "verified_at",
}

_WRITE_AUTH_MESSAGE = (
    "Google rechazó la autorización de escritura (drive.file): expiró, "
    f"fue revocada o no es válida. Vuelva a autorizar con: {AUTH_WRITE_COMMAND}"
)

_ALL_DRIVES = {"supportsAllDrives": True, "includeItemsFromAllDrives": True}


# Resultado -------------------------------------------------------------

UPLOADED = "uploaded"
VERIFIED = "verified"
RECOVERED = "recovered"
REJECTED = "rejected"
FAILED = "failed"
UNCERTAIN = "uncertain"
PLANNED = "planned"


@dataclass(frozen=True)
class UploadItem:
    file_id: str | None
    output_name: str
    status: str
    remote_id: str | None = None
    remote_name: str | None = None
    reason: str | None = None
    bytes: int = 0


@dataclass
class UploadSummary:
    source_folder_id: str
    target: queries.DriveFolder | None = None
    items: list[UploadItem] = field(default_factory=list)
    aborted: DriveError | None = None
    # True: se comprobó el acceso de escritura; None: no se comprobó.
    write_access: bool | None = None

    def count(self, status: str) -> int:
        return sum(item.status == status for item in self.items)

    @property
    def bytes_uploaded(self) -> int:
        return sum(item.bytes for item in self.items if item.status == UPLOADED)

    @property
    def complete(self) -> bool:
        return self.aborted is None and not any(
            item.status in (REJECTED, FAILED, UNCERTAIN) for item in self.items
        )


@dataclass(frozen=True)
class Candidate:
    """PNG de GD-03A que se puede subir."""

    entry: ManifestEntry
    path: Path

    @property
    def file_id(self) -> str:
        return self.entry.source.id


@dataclass
class Selection:
    candidates: list[Candidate] = field(default_factory=list)
    rejected: list[UploadItem] = field(default_factory=list)


class _Rejected(Exception):
    """El resultado no cumple la política: no se sube."""


class _Failed(Exception):
    """La subida o su verificación falló."""


class _AlreadyCreated(Exception):
    """Drive respondió 409: el ID preasignado ya tiene un archivo."""


# Selección local --------------------------------------------------------

def _revision_order(entry: ManifestEntry):
    drive = entry.source.drive
    version = drive.version
    number = int(version) if version and _VERSION.fullmatch(version) else -1
    return number, drive.modified_time or ""


def _png_problem(data: bytes, entry: ManifestEntry) -> str | None:
    """Motivo por el que data no es el PNG registrado, o None."""

    if hashlib.sha256(data).hexdigest() != entry.output_sha256:
        return (
            "el PNG no coincide con el SHA-256 registrado por GD-03A "
            "(alterado); no se sube."
        )

    identity = entry.identity
    options = OutputOptions(identity.width, identity.height, identity.background)

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)

            with Image.open(io.BytesIO(data)) as image:
                found = image.format, image.size, image.mode
                image.verify()
    except Exception:
        return "el archivo no es un PNG válido; no se sube."

    size, mode = expected_format(options)

    if found != ("PNG", size, mode):
        return (
            f"el archivo no es el PNG esperado ({size[0]} x {size[1]}, "
            f"{mode}); no se sube."
        )

    return None


def _read_png(path: Path, root: Path) -> bytes:
    """Bytes de un PNG propio de la carpeta de resultados."""

    if path.is_symlink():
        raise _Rejected("es un enlace simbólico; no se sube.")

    if not path.is_file():
        raise _Rejected("falta el PNG registrado; no se sube.")

    if path.resolve().parent != root:
        raise _Rejected("está fuera de la carpeta de resultados; no se sube.")

    return path.read_bytes()


def select_results(
    input_dir: Path,
    folder_id: str,
    model_name: str = DEFAULT_MODEL,
    options: OutputOptions | None = None,
) -> Selection:
    """
    PNG de GD-03A que se pueden subir, en el orden de GD-02 (nombre
    remoto y fileId). Solo lee archivos locales. Un manifiesto dañado
    lanza ManifestError.
    """
    options = options or OutputOptions()
    queries.validate_file_id(folder_id)
    input_dir = Path(input_dir)

    if not input_dir.is_dir():
        raise NotADirectoryError(f"No existe la carpeta: {input_dir}")

    manifest = Manifest.load(input_dir, clean=False)
    root = input_dir.resolve()
    known = manifest.known_names()
    selection = Selection()

    def reject(name, reason, file_id=None):
        selection.rejected.append(UploadItem(file_id, name, REJECTED, reason=reason))

    for path in sorted(input_dir.iterdir(), key=lambda item: item.name):
        if (
            path.name.upper().startswith(OUTPUT_PREFIX)
            and path.suffix.lower() == ".png"
            and name_key(path.name) not in known
        ):
            reject(
                path.name,
                "no figura en el manifiesto de GD-03A (sin procedencia); "
                "no se sube.",
            )

    groups: dict[str, list[ManifestEntry]] = {}
    wanted = (model_name, options.width, options.height, options.background, PIPELINE_VERSION)

    for entry in manifest.entries():
        identity = entry.identity

        if entry.source.kind != DRIVE_KIND:
            reject(
                entry.output_name,
                "es un resultado del lote local, sin procedencia de Drive; "
                "no se sube.",
            )
            continue

        if (
            identity.model, identity.width, identity.height,
            identity.background, identity.pipeline_version,
        ) != wanted:
            # Otra variante: se sube con sus propias opciones.
            continue

        file_id = entry.source.id

        if entry.source.drive.folder_id != folder_id:
            reject(
                entry.output_name,
                f"corresponde a otra carpeta de Drive "
                f"({entry.source.drive.folder_id}); no se sube.",
                file_id,
            )
            continue

        if entry.status != COMPLETED:
            reject(
                entry.output_name,
                "su registro de GD-03A quedó pendiente; vuelva a ejecutar "
                "process-local.",
                file_id,
            )
            continue

        groups.setdefault(file_id, []).append(entry)

    for file_id, group in groups.items():
        # Solo la revisión más reciente procesada de cada fileId.
        group.sort(key=_revision_order, reverse=True)

        if len(group) > 1 and _revision_order(group[0]) == _revision_order(group[1]):
            for entry in group:
                reject(
                    entry.output_name,
                    "hay varios resultados de este fileId sin una revisión "
                    "vigente clara; no se sube ninguno.",
                    file_id,
                )
            continue

        entry = group[0]
        path = input_dir / entry.output_name

        try:
            problem = _png_problem(_read_png(path, root), entry)
        except _Rejected as exc:
            problem = str(exc)
        except OSError:
            problem = "no se pudo leer el PNG; no se sube."

        if problem is not None:
            reject(entry.output_name, problem, file_id)
            continue

        selection.candidates.append(Candidate(entry, path))

    selection.candidates.sort(
        key=lambda candidate: (
            unicodedata.normalize("NFC", candidate.entry.source.name).casefold(),
            candidate.entry.source.name,
            candidate.file_id,
        )
    )

    return selection


# Identidad y nombres ----------------------------------------------------

def upload_key(entry: ManifestEntry, target_folder_id: str) -> str:
    """Identidad de la subida de un resultado a una carpeta."""

    identity = entry.identity
    text = "\n".join([
        entry.source.id,
        entry.source.sha256,
        entry.output_sha256,
        identity.model,
        str(identity.width),
        str(identity.height),
        identity.background,
        identity.pipeline_version,
        target_folder_id,
    ])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def remote_names(entry: ManifestEntry) -> tuple[str, str]:
    """Nombre base del PNG en Drive y su nombre de revisión."""

    identity = entry.identity
    name = entry.source.name
    stem = name.rpartition(".")[0] or name
    base = output_name(
        stem, OutputOptions(identity.width, identity.height, identity.background)
    )
    return base, with_revision(base, identity.revision())


def _app_properties(entry: ManifestEntry, key: str) -> dict:
    identity = entry.identity
    return {
        PROP_UPLOAD_KEY: key,
        PROP_SOURCE_FILE_ID: entry.source.id,
        PROP_SOURCE_SHA256: entry.source.sha256,
        PROP_OUTPUT_SHA256: entry.output_sha256,
        PROP_CONFIG: (
            f"{identity.model}|{identity.width}x{identity.height}|"
            f"{identity.background}|p{identity.pipeline_version}"
        ),
    }


# Registro local de subidas ----------------------------------------------

def ledger_path(input_dir: Path, target_folder_id: str, output_name: str) -> Path:
    # El fileId de la carpeta se codifica: no depende de mayúsculas.
    return (
        Path(input_dir) / LEDGER_DIR
        / downloads.encode_file_id(target_folder_id) / f"{output_name}.json"
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_ledger(path: Path) -> dict | None:
    if not path.exists():
        return None

    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        record = None

    valid = (
        isinstance(record, dict)
        and set(record) == _LEDGER_KEYS
        and type(record["schema_version"]) is int
        and record["schema_version"] == LEDGER_SCHEMA_VERSION
        and record["generator"] == GENERATOR
        and record["status"] in (LEDGER_PENDING, LEDGER_COMPLETED, LEDGER_MISMATCH)
        and isinstance(record["remote_id"], str)
        and isinstance(record["remote_name"], str)
        and isinstance(record["upload_key"], str)
    )

    if valid:
        try:
            queries.validate_file_id(record["remote_id"])
        except ValueError:
            valid = False

    if not valid:
        raise _Failed(
            f"el registro de subida {path.name} está dañado; no se sube para "
            "no duplicar el PNG. Revíselo antes de volver a intentarlo."
        )

    return record


def _write_ledger(path: Path, record: dict) -> None:
    """Escritura atómica (archivo propio de la aplicación)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(record, indent=2, ensure_ascii=False) + "\n").encode()
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")

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


# Drive ------------------------------------------------------------------

def _create_request(service, body: dict, data: bytes, chunk_size: int):
    """files.create reanudable de los bytes ya verificados."""

    media = MediaIoBaseUpload(
        io.BytesIO(data), mimetype=PNG_MIME, chunksize=chunk_size, resumable=True
    )
    return service.files().create(
        body=body, media_body=media, fields=REMOTE_FIELDS, supportsAllDrives=True
    )


def _write_error(error: DriveError) -> DriveError:
    """Mensajes propios de la autorización de escritura."""

    if isinstance(error, DriveAuthError):
        return DriveAuthError(_WRITE_AUTH_MESSAGE)

    if isinstance(error, DriveAccessError) and "storageQuotaExceeded" in str(error):
        return DriveAccessError(
            "El almacenamiento de Google Drive de la cuenta está lleno "
            "(403: storageQuotaExceeded). No se subió el PNG."
        )

    return error


@dataclass
class _Context:
    read: object
    own: object
    write: object | None
    execute: object
    target: queries.DriveFolder
    folder_id: str
    input_dir: Path
    names: set[str]
    chunk_size: int
    sleep: object
    dry_run: bool

    def run_own(self, request):
        """Solicitud con la credencial de los PNG propios."""

        try:
            return self.execute(request)
        except DriveError as exc:
            if self.own is not self.write:
                raise
            raise _write_error(exc) from None


def _list(service, run, query: str, fields: str) -> list[dict]:
    """Todos los archivos de una consulta, página por página."""

    items, token, seen = [], None, set()

    while True:
        response = run(service.files().list(
            q=query, fields=f"nextPageToken, files({fields})",
            pageSize=PAGE_SIZE, pageToken=token, **_ALL_DRIVES,
        ))
        items.extend(response.get("files") or [])
        token = response.get("nextPageToken")

        if not token:
            return items

        if token in seen:
            raise DriveResponseError("Google Drive repitió un token de paginación.")

        seen.add(token)


def _get_own(ctx: _Context, file_id: str) -> dict | None:
    try:
        return ctx.run_own(ctx.own.files().get(
            fileId=file_id, fields=REMOTE_FIELDS, supportsAllDrives=True
        ))
    except DriveNotFoundError:
        return None


def _check_write_access(ctx: _Context) -> None:
    target = ctx.target

    try:
        item = ctx.run_own(ctx.write.files().get(
            fileId=target.id,
            fields="id, mimeType, trashed, capabilities(canAddChildren)",
            supportsAllDrives=True,
        ))
    except DriveNotFoundError:
        item = None

    if item is None or item.get("trashed"):
        raise DriveWriteAccessError(
            f"La autorización de escritura (drive.file) no tiene acceso a la "
            f"carpeta de destino {target.name} ({target.id}). Con drive.file "
            "la aplicación solo puede crear archivos en carpetas que ella "
            "creó o que el usuario le autorizó (por ejemplo, con Google "
            "Picker, todavía no integrado). No se subió nada y no se "
            "solicita el permiso 'drive'."
        )

    if (item.get("capabilities") or {}).get("canAddChildren") is False:
        raise DriveWriteAccessError(
            f"La cuenta no puede agregar archivos a la carpeta {target.name} "
            f"({target.id}). No se subió nada."
        )


@dataclass(frozen=True)
class _Problem:
    text: str
    uncertain: bool = False


def _remote_problem(remote: dict, name: str | None, ctx: _Context, expected: dict) -> _Problem | None:
    """Motivo por el que el PNG remoto no es el esperado, o None."""

    if remote.get("trashed"):
        return _Problem("el PNG remoto está en la papelera; no se vuelve a subir.")

    if name is not None and remote.get("name") != name:
        return _Problem(f"el PNG remoto no tiene el nombre esperado ({name}).")

    if ctx.target.id not in (remote.get("parents") or []):
        return _Problem("el PNG remoto no está en la carpeta de destino.")

    if remote.get("mimeType") != PNG_MIME:
        return _Problem("el archivo remoto no es image/png.")

    if (remote.get("appProperties") or {}).get(PROP_UPLOAD_KEY) != expected["key"]:
        return _Problem("el PNG remoto no tiene la identidad de este resultado.")

    md5 = remote.get("md5Checksum")

    if remote.get("size") is None or md5 is None:
        return _Problem(
            "Drive todavía no informó el tamaño o el MD5 del PNG.", uncertain=True
        )

    if str(remote["size"]) != str(expected["size"]):
        return _Problem("el tamaño del PNG remoto no coincide con el local.")

    if md5 != expected["md5"]:
        return _Problem("el MD5 del PNG remoto no coincide con el local.")

    sha256 = remote.get("sha256Checksum")

    if sha256 is not None and sha256 != expected["sha256"]:
        return _Problem("el SHA-256 del PNG remoto no coincide con el local.")

    return None


def _send(ctx: _Context, body: dict, data: bytes) -> None:
    """
    Crea el archivo con el ID preasignado del cuerpo. Los reintentos
    reinician la sesión con el mismo ID: si un intento anterior llegó
    a Drive, la respuesta es 409 (_AlreadyCreated) y no hay duplicado.
    """
    retries = 0

    while True:
        request = _create_request(ctx.write, body, data, ctx.chunk_size)

        try:
            response = None
            while response is None:
                _, response = request.next_chunk(num_retries=0)
            return

        except Exception as exc:
            if isinstance(exc, HttpError) and exc.status_code == 409:
                raise _AlreadyCreated() from None

            translated = client.translate_error(exc)

            if translated is None:
                raise

        if not translated.retryable or retries >= MAX_UPLOAD_RETRIES:
            raise _write_error(translated.error) from None

        retries += 1
        ctx.sleep(translated.retry_after or client.backoff(retries))


def _ledger_record(ctx, candidate, key, remote_id, name, size, md5, status):
    entry = candidate.entry
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "generator": GENERATOR,
        "status": status,
        "upload_key": key,
        "remote_id": remote_id,
        "remote_name": name,
        "target_folder_id": ctx.target.id,
        "source_file_id": entry.source.id,
        "source_folder_id": entry.source.drive.folder_id,
        "output_name": entry.output_name,
        "output_sha256": entry.output_sha256,
        "size": size,
        "md5_checksum": md5,
        "recorded_at": _now(),
        "verified_at": _now() if status == LEDGER_COMPLETED else None,
    }


def _check_original(ctx: _Context, entry: ManifestEntry) -> None:
    """El resultado corresponde a un original vigente de la carpeta."""

    try:
        original = ctx.execute(ctx.read.files().get(
            fileId=entry.source.id, fields=_ORIGINAL_FIELDS, supportsAllDrives=True
        ))
    except DriveNotFoundError:
        raise _Rejected("el original ya no existe en Drive; no se sube.") from None

    if original.get("trashed") or ctx.folder_id not in (original.get("parents") or []):
        raise _Rejected(
            "el original ya no está en la carpeta de Drive indicada; no se sube."
        )

    if original.get("md5Checksum") != entry.source.drive.md5_checksum:
        raise _Rejected(
            "el original cambió en Drive desde su descarga; descargue y "
            "procese la nueva revisión antes de subir."
        )


def _create_and_verify(ctx, candidate, data, expected, remote_id, name, path) -> UploadItem:
    entry = candidate.entry
    item = dict(file_id=candidate.file_id, output_name=entry.output_name,
                remote_id=remote_id, remote_name=name)
    body = {
        "id": remote_id,
        "name": name,
        "parents": [ctx.target.id],
        "mimeType": PNG_MIME,
        "appProperties": _app_properties(entry, expected["key"]),
        "description": (
            f"Generado por drive-image-processor a partir de "
            f"{entry.source.name} ({entry.source.id})."
        ),
    }
    uncertain = (
        "el estado de la subida quedó incierto. La próxima ejecución lo "
        "comprobará con el ID reservado; nunca se crea otro archivo."
    )

    try:
        _send(ctx, body, data)
    except _AlreadyCreated:
        pass
    except DriveCertificateError:
        raise
    except DriveTransientError as exc:
        # Respuesta ambigua: ¿llegó a Drive? Se consulta el ID reservado.
        try:
            if _get_own(ctx, remote_id) is None:
                return UploadItem(status=UNCERTAIN, reason=f"{exc} {uncertain}", **item)
        except DriveTransientError:
            return UploadItem(status=UNCERTAIN, reason=f"{exc} {uncertain}", **item)

    try:
        remote = _get_own(ctx, remote_id)
    except DriveTransientError as exc:
        return UploadItem(status=UNCERTAIN, reason=f"{exc} {uncertain}", **item)

    if remote is None:
        return UploadItem(
            status=UNCERTAIN, reason=f"Drive no confirma el PNG creado; {uncertain}", **item
        )

    problem = _remote_problem(remote, name, ctx, expected)

    if problem is not None and problem.uncertain:
        return UploadItem(status=UNCERTAIN, reason=f"{problem.text} {uncertain}", **item)

    status = LEDGER_MISMATCH if problem else LEDGER_COMPLETED
    _write_ledger(path, _ledger_record(
        ctx, candidate, expected["key"], remote_id, name,
        expected["size"], expected["md5"], status,
    ))

    if problem is not None:
        return UploadItem(
            status=FAILED,
            reason=(
                f"el PNG se creó en Drive, pero {problem.text} No se da por "
                "completado; revíselo en Drive (no se crea otro)."
            ),
            **item,
        )

    return UploadItem(status=UPLOADED, bytes=len(data), **item)


def _upload_one(ctx: _Context, candidate: Candidate) -> UploadItem:
    entry = candidate.entry
    item = dict(file_id=candidate.file_id, output_name=entry.output_name)

    # Se suben exactamente los bytes verificados.
    data = _read_png(candidate.path, ctx.input_dir.resolve())
    problem = _png_problem(data, entry)

    if problem is not None:
        raise _Rejected(problem)

    _check_original(ctx, entry)

    key = upload_key(entry, ctx.target.id)
    expected = {
        "key": key,
        "size": len(data),
        "md5": hashlib.md5(data, usedforsecurity=False).hexdigest(),
        "sha256": entry.output_sha256,
    }
    path = ledger_path(ctx.input_dir, ctx.target.id, entry.output_name)
    record = _read_ledger(path)

    if record is not None:
        if record["upload_key"] != key:
            raise _Failed(
                "el registro de subida corresponde a otro resultado; no se sube."
            )

        remote_id, name = record["remote_id"], record["remote_name"]
        remote = _get_own(ctx, remote_id)

        if remote is not None:
            problem = _remote_problem(remote, name, ctx, expected)

            if problem is None:
                if record["status"] == LEDGER_COMPLETED:
                    return UploadItem(status=VERIFIED, remote_id=remote_id, remote_name=name, **item)

                if not ctx.dry_run:
                    _write_ledger(path, _ledger_record(
                        ctx, candidate, key, remote_id, name,
                        expected["size"], expected["md5"], LEDGER_COMPLETED,
                    ))

                return UploadItem(
                    status=RECOVERED, remote_id=remote_id, remote_name=name,
                    reason="se confirmó una subida que había quedado incierta.",
                    **item,
                )

            if problem.uncertain:
                return UploadItem(status=UNCERTAIN, remote_id=remote_id,
                                  remote_name=name, reason=problem.text, **item)

            if not ctx.dry_run and record["status"] != LEDGER_MISMATCH:
                _write_ledger(path, dict(record, status=LEDGER_MISMATCH, recorded_at=_now()))

            raise _Failed(f"{problem.text} No se sube otro PNG.")

        if record["status"] != LEDGER_PENDING:
            raise _Failed(
                "el PNG subido antes ya no existe en Drive o no es accesible; "
                "no se vuelve a subir automáticamente. Si lo eliminó a "
                f"propósito, borre {LEDGER_DIR}/.../{path.name} para subirlo "
                "de nuevo."
            )

        # Pendiente que no llegó a Drive: se reintenta con el mismo ID.
        if ctx.dry_run:
            return UploadItem(
                status=PLANNED, remote_id=remote_id, remote_name=name,
                reason="se reintentaría con el ID ya reservado.", **item,
            )

        return _create_and_verify(ctx, candidate, data, expected, remote_id, name, path)

    # Sin registro: ¿ya está en Drive? (por ejemplo, si se perdió el registro)
    found = _list(
        ctx.own, ctx.run_own,
        f"'{ctx.target.id}' in parents and trashed = false and "
        f"appProperties has {{ key='{PROP_UPLOAD_KEY}' and value='{key}' }}",
        REMOTE_FIELDS,
    )

    if len(found) > 1:
        raise _Failed(
            f"hay {len(found)} PNG con esta identidad en la carpeta de "
            "destino; no se sube otro."
        )

    if found:
        remote = found[0]
        problem = _remote_problem(remote, None, ctx, expected)

        if problem is not None:
            raise _Failed(f"{problem.text} No se sube otro PNG.")

        if not ctx.dry_run:
            _write_ledger(path, _ledger_record(
                ctx, candidate, key, remote["id"], remote.get("name"),
                expected["size"], expected["md5"], LEDGER_COMPLETED,
            ))

        return UploadItem(
            status=VERIFIED, remote_id=remote["id"], remote_name=remote.get("name"),
            reason="ya estaba en Drive.", **item,
        )

    base, revision = remote_names(entry)

    if name_key(base) not in ctx.names:
        name = base
    elif name_key(revision) not in ctx.names:
        name = revision
    else:
        raise _Rejected(
            f"ya existen archivos con los nombres {base} y {revision} en la "
            "carpeta de destino; no se sobrescriben ni se crea otro nombre."
        )

    if ctx.dry_run:
        return UploadItem(status=PLANNED, remote_name=name, **item)

    response = ctx.run_own(ctx.write.files().generateIds(count=1, space="drive", type="files"))
    ids = response.get("ids") if isinstance(response, dict) else None

    if not isinstance(ids, list) or len(ids) != 1:
        raise _Failed("Google Drive no devolvió un ID preasignado.")

    remote_id = queries.validate_file_id(ids[0])

    # El ID queda registrado antes de crear: ninguna respuesta perdida
    # puede llevar a crear un segundo archivo.
    _write_ledger(path, _ledger_record(
        ctx, candidate, key, remote_id, name,
        expected["size"], expected["md5"], LEDGER_PENDING,
    ))
    ctx.names.add(name_key(name))

    return _create_and_verify(ctx, candidate, data, expected, remote_id, name, path)


def upload_results(
    read_service,
    write_service,
    input_dir: Path,
    folder_id: str,
    *,
    target_folder_id: str | None = None,
    model_name: str = DEFAULT_MODEL,
    options: OutputOptions | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    sleep=None,
    execute=None,
    on_item=None,
) -> UploadSummary:
    """
    Sube los PNG verificados de input_dir cuyo original es de
    folder_id, a target_folder_id (por defecto, la misma carpeta).

    write_service puede ser None solo en la vista previa. limit cuenta
    los PNG que se pueden subir, en el orden de GD-02; los rechazos se
    informan siempre. dry_run no escribe nada, ni en Drive ni en disco.

    Antes de cualquier consulta a Drive valida la selección local
    (ManifestError si el manifiesto está dañado). Una carpeta sin
    acceso de escritura lanza DriveWriteAccessError antes de subir.
    """
    if limit is not None and limit < 1:
        raise ValueError("El límite debe ser al menos 1.")

    if write_service is None and not dry_run:
        raise DriveAuthError(
            "Falta la autorización de escritura (drive.file). Autorice con: "
            f"{AUTH_WRITE_COMMAND}"
        )

    options = options or OutputOptions()
    execute = execute or client.execute
    sleep = sleep or time.sleep
    target_folder_id = target_folder_id or folder_id
    queries.validate_file_id(target_folder_id)
    input_dir = Path(input_dir)

    selection = select_results(input_dir, folder_id, model_name, options)
    candidates = selection.candidates[:limit] if limit is not None else selection.candidates

    summary = UploadSummary(source_folder_id=folder_id)
    summary.target = queries.get_folder(read_service, target_folder_id, execute=execute)

    ctx = _Context(
        read=read_service,
        own=write_service or read_service,
        write=write_service,
        execute=execute,
        target=summary.target,
        folder_id=folder_id,
        input_dir=input_dir,
        names=set(),
        chunk_size=chunk_size,
        sleep=sleep,
        dry_run=dry_run,
    )

    if write_service is not None:
        _check_write_access(ctx)
        summary.write_access = True

    # Todos los nombres del destino, también los ajenos (credencial de lectura).
    ctx.names = {
        name_key(item.get("name") or "")
        for item in _list(
            read_service, execute,
            f"'{summary.target.id}' in parents and trashed = false", "id, name",
        )
    }

    def report(item):
        summary.items.append(item)
        if on_item is not None:
            on_item(item)

    for item in selection.rejected:
        report(item)

    consecutive = 0

    for candidate in candidates:
        base = dict(file_id=candidate.file_id, output_name=candidate.entry.output_name)
        transient = False

        try:
            item = _upload_one(ctx, candidate)
        except _Rejected as exc:
            item = UploadItem(status=REJECTED, reason=str(exc), **base)
        except _Failed as exc:
            item = UploadItem(status=FAILED, reason=str(exc), **base)
        except OSError as exc:
            item = UploadItem(
                status=FAILED,
                reason=f"error local ({type(exc).__name__}); no se subió el PNG.",
                **base,
            )
        except (DriveAuthError, DriveAccessError, DriveCertificateError) as exc:
            # Afectan a todo el lote: se detiene.
            report(UploadItem(status=FAILED, reason=str(exc), **base))
            summary.aborted = exc
            break
        except DriveError as exc:
            item = UploadItem(status=FAILED, reason=str(exc), **base)
            transient = isinstance(exc, DriveTransientError)

        report(item)

        if transient or item.status == UNCERTAIN:
            consecutive += 1
        else:
            consecutive = 0

        if consecutive >= MAX_CONSECUTIVE_TRANSIENT_FAILURES:
            summary.aborted = DriveTransientError(
                f"Se detuvo la subida después de {consecutive} fallos de red "
                "seguidos."
            )
            break

    return summary
