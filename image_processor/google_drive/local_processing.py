"""
Procesamiento local de las imágenes descargadas por GD-02 (GD-03A).

Sin red ni credenciales: solo lee la carpeta de descargas y escribe
en la de resultados, con el mismo motor, la misma política de nombres
(H-04) y el mismo manifiesto (H-05) que el lote local. Nunca modifica
las imágenes descargadas ni sus registros.

Procedencia
-----------

Solo se procesa una imagen con un registro completo de GD-02 en
.drive/<nombre>.json que:

- pertenece a la carpeta de Drive seleccionada (las de otras carpetas
  se excluyen);
- nombra este mismo archivo, cuyo nombre local se deriva del fileId y
  del nombre remoto registrados: un registro no acredita a otra imagen;
- coincide en tamaño, MD5 y SHA-256 con el archivo.

Una imagen sin registro, con un registro dañado o alterada se rechaza.
Los enlaces simbólicos también: ninguna ruta sale de la carpeta.

Identidad y nombres
-------------------

El original es ("drive", fileId exacto): el fileId es opaco y nunca se
normaliza. Si hay varias revisiones descargadas del mismo fileId, se
procesa la vigente (mayor versión de Drive); los resultados de las
anteriores se conservan.

El resultado se llama TRANSFORMADA_<nombre local>[variante].png. El
nombre local de GD-02 ya incluye el fileId codificado, así que dos
archivos remotos con el mismo nombre nunca comparten resultado.

Preparación para GD-03B
-----------------------

prepared_results() indica, por cada fotografía, qué PNG le corresponde,
la carpeta de Drive de destino (la de origen) y si el resultado está
completo y verificado por su SHA-256. No sube nada.
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .. import batch
from ..manifest import (
    COMPLETED,
    DRIVE_KIND,
    DriveProvenance,
    Manifest,
    SourceRecord,
    file_sha256,
)
from ..naming import OUTPUT_PREFIX, SUPPORTED_EXTENSIONS
from ..processing import OutputOptions
from . import downloads, queries


DEFAULT_MODEL = "isnet-general-use"

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(r"[0-9]+")


class ForeignFolderError(RuntimeError):
    """El destino ya contiene resultados de otra carpeta de Drive."""


@dataclass(frozen=True)
class DriveSource:
    """Imagen descargada cuya procedencia se verificó."""

    path: Path
    record: SourceRecord

    @property
    def file_id(self) -> str:
        return self.record.id

    @property
    def remote_name(self) -> str:
        return self.record.name


@dataclass(frozen=True)
class Excluded:
    """Imagen local que no se procesa, y por qué."""

    name: str
    reason: str
    file_id: str | None = None


@dataclass
class Discovery:
    sources: list[DriveSource] = field(default_factory=list)
    # Sin procedencia válida o alteradas: impiden un lote completo.
    rejected: list[Excluded] = field(default_factory=list)
    # De otra carpeta de Drive: no se mezclan.
    other_folder: list[Excluded] = field(default_factory=list)
    # Revisiones anteriores de un fileId con una más reciente.
    superseded: list[Excluded] = field(default_factory=list)
    # Todas las descargas válidas de la carpeta, antes de elegir la
    # revisión vigente de cada fileId (para una selección explícita).
    valid: list[DriveSource] = field(default_factory=list)


@dataclass(frozen=True)
class PreparedResult:
    """Estado del resultado de una fotografía de Drive (para GD-03B)."""

    file_id: str
    folder_id: str
    remote_name: str
    source_name: str
    source_sha256: str
    md5_checksum: str
    output_name: str | None
    output_path: Path | None
    output_sha256: str | None
    ready: bool
    reason: str | None = None


@dataclass
class LocalProcessingResult:
    folder_id: str
    discovery: Discovery
    selected: list[DriveSource]
    batch: batch.BatchResult
    prepared: list[PreparedResult]
    # Con una selección explícita: las descargas pedidas que no están o
    # no son válidas, y los rechazos que corresponden a la selección.
    unavailable: list[Excluded] = field(default_factory=list)
    scoped_rejected: list[Excluded] | None = None

    @property
    def rejected(self) -> list[Excluded]:
        """Rechazos que impiden un lote completo."""

        if self.scoped_rejected is None:
            return self.discovery.rejected
        return self.scoped_rejected + self.unavailable

    @property
    def complete(self) -> bool:
        """Todo lo seleccionado quedó procesado o verificado."""

        result = self.batch
        return not (
            self.rejected
            or result.failed
            or result.unverified
            or result.unregistered
        )


# Procedencia --------------------------------------------------------------

def _is_candidate(path: Path) -> bool:
    name = path.name
    return (
        not name.startswith(".")
        and path.suffix.lower() in SUPPORTED_EXTENSIONS
        and not name.upper().startswith(OUTPUT_PREFIX)
    )


def _is_optional_text(value) -> bool:
    return value is None or (isinstance(value, str) and value != "")


def _record_problem(record: dict, name: str, extension: str) -> str | None:
    """Motivo por el que el registro no acredita a esta imagen, o None."""

    if (
        type(record["schema_version"]) is not int
        or record["generator"] != downloads.GENERATOR
        or type(record["size"]) is not int
        or record["size"] <= 0
        or not isinstance(record["name"], str)
        or not record["name"]
        or not isinstance(record["md5_checksum"], str)
        or _HEX32.fullmatch(record["md5_checksum"]) is None
        or not (
            record["drive_sha256"] is None
            or (
                isinstance(record["drive_sha256"], str)
                and _HEX64.fullmatch(record["drive_sha256"]) is not None
            )
        )
        or not all(
            _is_optional_text(record[key])
            for key in ("version", "head_revision_id", "modified_time")
        )
    ):
        return "el registro de procedencia está dañado."

    try:
        queries.validate_file_id(record["file_id"])
        queries.validate_file_id(record["folder_id"])
    except ValueError:
        return "el registro de procedencia tiene un identificador inválido."

    if record["status"] != downloads.COMPLETED:
        return (
            "la descarga no se completó (registro pendiente); vuelva a "
            "ejecutar download."
        )

    mime = record["mime_type"]

    if not isinstance(mime, str) or extension not in downloads.EXTENSIONS_BY_MIME.get(mime, ()):
        return "el tipo registrado no corresponde a la imagen."

    # El nombre local se deriva del fileId: el registro es de esta imagen.
    base = downloads.local_name(record["name"], record["file_id"])

    if name not in (base, downloads.revision_name(base, record["md5_checksum"])):
        return "el registro de procedencia corresponde a otro archivo de Drive."

    return None


def _content_problem(path: Path, record: dict) -> str | None:
    """Motivo por el que el archivo no coincide con su registro, o None."""

    if path.stat().st_size != record["size"]:
        return "su tamaño"

    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()

    with path.open("rb") as file:
        for block in iter(lambda: file.read(downloads.MIB), b""):
            md5.update(block)
            sha256.update(block)

    if md5.hexdigest() != record["md5_checksum"]:
        return "su MD5"

    if sha256.hexdigest() != record["sha256"]:
        return "su SHA-256"

    if record["drive_sha256"] not in (None, record["sha256"]):
        return "el SHA-256 de Drive"

    return None


def _source_record(record: dict, name: str, size: int) -> SourceRecord:
    return SourceRecord(
        kind=DRIVE_KIND,
        id=record["file_id"],
        name=record["name"],
        size=size,
        sha256=record["sha256"],
        drive=DriveProvenance(
            folder_id=record["folder_id"],
            md5_checksum=record["md5_checksum"],
            head_revision_id=record["head_revision_id"],
            version=record["version"],
            modified_time=record["modified_time"],
            local_name=name,
        ),
    )


def _revision_order(source: DriveSource):
    drive = source.record.drive
    version = drive.version
    number = int(version) if version and _VERSION.fullmatch(version) else -1
    return number, drive.modified_time or ""


def _select_current(sources: list[DriveSource], discovery: Discovery) -> list[DriveSource]:
    """Una revisión vigente por fileId (comparado exacto)."""

    groups: dict[str, list[DriveSource]] = {}

    for source in sources:
        groups.setdefault(source.file_id, []).append(source)

    current = []

    for file_id, group in groups.items():
        group.sort(key=_revision_order, reverse=True)
        newest = _revision_order(group[0])
        tied = [source for source in group if _revision_order(source) == newest]

        if len(tied) > 1:
            for source in tied:
                discovery.rejected.append(Excluded(
                    source.path.name,
                    "hay varias descargas de este fileId sin una revisión "
                    "vigente clara.",
                    file_id,
                ))
        else:
            current.append(group[0])

        for source in group[len(tied):]:
            discovery.superseded.append(Excluded(
                source.path.name,
                "hay una revisión más reciente descargada; se conserva su "
                "resultado anterior.",
                file_id,
            ))

    return current


def discover_downloads(input_dir: Path, folder_id: str) -> Discovery:
    """
    Imágenes de input_dir con procedencia válida de GD-02 para
    folder_id, en el orden de GD-02 (nombre remoto y fileId). Solo
    lee archivos locales.
    """
    queries.validate_file_id(folder_id)
    input_dir = Path(input_dir)

    if not input_dir.is_dir():
        raise NotADirectoryError(f"No existe la carpeta: {input_dir}")

    root = input_dir.resolve()
    discovery = Discovery()
    verified = []

    for path in sorted(input_dir.iterdir(), key=lambda item: item.name):
        if not _is_candidate(path):
            continue

        name = path.name

        if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
            if path.is_symlink() or path.exists():
                discovery.rejected.append(Excluded(
                    name, "no es un archivo propio de la carpeta (enlace); no se procesa."
                ))
            continue

        try:
            record = downloads.read_record(input_dir, name)
        except downloads.InvalidRecordError:
            discovery.rejected.append(Excluded(
                name, "el registro de procedencia está dañado."
            ))
            continue

        if record is None:
            discovery.rejected.append(Excluded(
                name,
                f"no tiene registro de procedencia de GD-02 "
                f"({downloads.METADATA_DIR}/); no se procesa.",
            ))
            continue

        problem = _record_problem(record, name, path.suffix.lower())

        if problem is not None:
            discovery.rejected.append(Excluded(name, problem))
            continue

        file_id = record["file_id"]

        if record["folder_id"] != folder_id:
            discovery.other_folder.append(Excluded(
                name, "pertenece a otra carpeta de Drive.", file_id
            ))
            continue

        problem = _content_problem(path, record)

        if problem is not None:
            discovery.rejected.append(Excluded(
                name,
                f"la imagen local no coincide con su registro de GD-02 "
                f"({problem}): alterada o incompleta; no se procesa.",
                file_id,
            ))
            continue

        verified.append(DriveSource(path, _source_record(record, name, record["size"])))

    discovery.valid = list(verified)
    discovery.sources = sorted(
        _select_current(verified, discovery),
        key=lambda source: (
            unicodedata.normalize("NFC", source.remote_name).casefold(),
            source.remote_name,
            source.file_id,
        ),
    )

    return discovery


# Resultados -----------------------------------------------------------------

def check_output_folder(output_dir: Path, folder_id: str) -> None:
    """
    Un destino solo guarda resultados de una carpeta de Drive. Solo
    lee: lanza ManifestError o ForeignFolderError.
    """

    if not output_dir.is_dir():
        return

    others = sorted({
        entry.source.drive.folder_id
        for entry in Manifest.load(output_dir, clean=False).entries()
        if entry.source.kind == DRIVE_KIND
        and entry.source.drive.folder_id != folder_id
    })

    if others:
        raise ForeignFolderError(
            f"El destino {output_dir} ya contiene resultados de otra carpeta "
            f"de Drive ({', '.join(others)}). No se mezclan carpetas: use "
            "otro destino."
        )


def prepared_results(
    output_dir: Path,
    sources: list[DriveSource],
    model_name: str = DEFAULT_MODEL,
    options: OutputOptions | None = None,
) -> list[PreparedResult]:
    """
    Resultado de cada fotografía con esta configuración, según el
    manifiesto del destino. ready exige el registro completo y que el
    PNG conserve el SHA-256 registrado. Solo lee.
    """
    options = options or OutputOptions()
    output_dir = Path(output_dir)
    manifest = (
        Manifest.load(output_dir, clean=False)
        if output_dir.is_dir()
        else Manifest(output_dir)
    )

    prepared = []

    for source in sources:
        record = source.record
        identity = batch.result_identity(record, model_name, options)
        matches = sorted(
            (
                entry for entry in manifest.entries()
                if entry.identity == identity
                and entry.source.drive is not None
                and entry.source.drive.folder_id == record.drive.folder_id
            ),
            key=lambda entry: entry.status != COMPLETED,
        )

        entry = matches[0] if matches else None
        reason = None

        if entry is None:
            reason = "todavía no tiene resultado."
        elif entry.status != COMPLETED:
            reason = "su registro quedó pendiente."
        else:
            path = output_dir / entry.output_name

            try:
                if file_sha256(path) != entry.output_sha256:
                    reason = "el PNG no coincide con su registro."
            except OSError:
                reason = "falta el PNG registrado."

        prepared.append(PreparedResult(
            file_id=record.id,
            folder_id=record.drive.folder_id,
            remote_name=record.name,
            source_name=source.path.name,
            source_sha256=record.sha256,
            md5_checksum=record.drive.md5_checksum,
            output_name=entry.output_name if entry else None,
            output_path=output_dir / entry.output_name if entry else None,
            output_sha256=entry.output_sha256 if entry else None,
            ready=reason is None,
            reason=reason,
        ))

    return prepared


def process_downloads(
    input_dir: Path,
    output_dir: Path,
    folder_id: str,
    *,
    model_name: str = DEFAULT_MODEL,
    options: OutputOptions | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    on_item=None,
    selection=None,
) -> LocalProcessingResult:
    """
    Procesa las descargas verificadas de folder_id con el lote común
    (batch.process_sources). limit cuenta las imágenes seleccionadas,
    en el orden de GD-02; los rechazos se informan siempre.

    selection (sync, GD-04) es una lista de pares (fileId, nombre
    local): se procesan exactamente esas descargas, en ese orden, sin
    elegir la revisión por versión. Una descarga pedida que no está o
    no es válida se informa en unavailable. Solo cuentan los rechazos
    de los fileId pedidos. No se combina con limit.

    on_item(DriveSource, batch.ItemResult) recibe el estado de cada
    imagen. Lanza ForeignFolderError o ManifestError antes de procesar.
    """
    if limit is not None and limit < 1:
        raise ValueError("El límite debe ser al menos 1.")

    if limit is not None and selection is not None:
        raise ValueError("limit y selection no se combinan.")

    options = options or OutputOptions()
    output_dir = Path(output_dir)

    discovery = discover_downloads(input_dir, folder_id)
    unavailable: list[Excluded] = []
    scoped_rejected = None

    if selection is None:
        selected = discovery.sources[:limit] if limit is not None else list(discovery.sources)
    else:
        pairs = list(selection)
        wanted = {file_id for file_id, _ in pairs}
        # Por el nombre exacto: en Windows, Path no distingue mayúsculas.
        valid = {(source.file_id, source.path.name): source for source in discovery.valid}
        selected = []

        for file_id, name in pairs:
            source = valid.get((file_id, name))

            if source is None:
                unavailable.append(Excluded(
                    name,
                    "la descarga verificada no está disponible o ya no es "
                    "válida; no se procesa otra revisión en su lugar.",
                    file_id,
                ))
            else:
                selected.append(source)

        scoped_rejected = [
            item for item in discovery.rejected if item.file_id in wanted
        ]

    check_output_folder(output_dir, folder_id)

    # Se busca por el texto exacto de la ruta: en Windows, Path compara
    # sin distinguir mayúsculas.
    by_path = {str(source.path): source for source in selected}

    def report(item):
        if on_item is not None:
            on_item(by_path[str(item.source)], item)

    result = batch.process_sources(
        [source.path for source in selected],
        output_dir,
        describe=lambda path: by_path[str(path)].record,
        model_name=model_name,
        options=options,
        dry_run=dry_run,
        on_item=report,
    )

    return LocalProcessingResult(
        folder_id=folder_id,
        discovery=discovery,
        selected=selected,
        batch=result,
        prepared=prepared_results(output_dir, selected, model_name, options),
        unavailable=unavailable,
        scoped_rejected=scoped_rejected,
    )
