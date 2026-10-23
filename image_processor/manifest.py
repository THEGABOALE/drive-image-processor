"""
Manifiesto persistente de resultados.

Cada carpeta de destino guarda un archivo JSON versionado que
registra la procedencia de cada resultado: el original (contenido
y ubicación), el modelo, las opciones y la versión del
procesamiento, junto con el SHA-256 del PNG generado.

El manifiesto es el único archivo que la aplicación reemplaza:
se escribe en un temporal y se sustituye con os.replace(). Las
imágenes de productos nunca se reemplazan (ver atomic_write).

No se admiten lotes concurrentes sobre el mismo destino.
"""

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import sleep

from .atomic_write import (
    PUBLISH_RETRY_DELAYS,
    STALE_TEMP_AGE_SECONDS,
    TEMP_SUFFIX,
)
from .naming import OUTPUT_PREFIX, name_key
from .processing import BACKGROUNDS


MANIFEST_NAME = ".transformada_manifest.json"
SCHEMA_VERSION = 1
GENERATOR = "drive-image-processor"

PENDING = "pending"
COMPLETED = "completed"
STATUSES = (PENDING, COMPLETED)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_MANIFEST_TEMP = re.compile(
    re.escape(MANIFEST_NAME) + r"\.[a-z0-9_]{8}" + re.escape(TEMP_SUFFIX)
)

_TOP_LEVEL_KEYS = {"schema_version", "generator", "entries"}
_ENTRY_KEYS = {
    "output_name",
    "status",
    "output_sha256",
    "source",
    "model",
    "options",
    "pipeline_version",
    "recorded_at",
}
_SOURCE_KEYS = {"kind", "id", "name", "size", "sha256"}
_OPTION_KEYS = {"width", "height", "background"}


class ManifestError(RuntimeError):
    """El manifiesto no se puede leer, validar o guardar."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()

    with Path(path).open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


@dataclass(frozen=True)
class ResultIdentity:
    """Lo que determina el contenido de un resultado."""

    source_sha256: str
    model: str
    width: int
    height: int
    background: str
    pipeline_version: str

    def revision(self) -> str:
        """
        Etiqueta determinista para nombres de revisión.

        Las opciones ya forman parte del nombre base; la revisión
        distingue el contenido del original, el modelo y la
        versión del procesamiento.
        """
        text = "\n".join(
            [self.source_sha256, self.model, self.pipeline_version]
        )

        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class SourceRecord:
    """
    Procedencia del original. kind distingue el origen ("local";
    más adelante, Google Drive) e id lo identifica dentro de él.
    """

    kind: str
    id: str
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ManifestEntry:
    output_name: str
    status: str
    identity: ResultIdentity
    source: SourceRecord
    output_sha256: str | None
    recorded_at: str


def local_source_id(source: Path, output_dir: Path) -> str:
    """
    Ruta del original relativa a la carpeta de destino, con "/".
    Si están en unidades distintas de Windows, la ruta absoluta.
    """
    try:
        relative = os.path.relpath(source, output_dir)
    except ValueError:
        relative = os.path.abspath(source)

    return Path(relative).as_posix()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ManifestError(f"Manifiesto inválido: {message}")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_text(value) -> bool:
    return isinstance(value, str) and value != ""


def _is_safe_output_name(name) -> bool:
    return (
        _is_text(name)
        and Path(name).name == name
        and "/" not in name
        and "\\" not in name
        and name.upper().startswith(OUTPUT_PREFIX)
        and name.lower().endswith(".png")
    )


def _parse_entry(raw, index: int) -> ManifestEntry:
    where = f"entrada {index}"

    _require(isinstance(raw, dict), f"{where} no es un objeto")
    _require(set(raw) == _ENTRY_KEYS, f"{where} tiene campos inesperados")

    _require(
        _is_safe_output_name(raw["output_name"]),
        f"{where}: output_name no es un nombre de resultado válido",
    )
    _require(raw["status"] in STATUSES, f"{where}: estado desconocido")

    output_sha256 = raw["output_sha256"]

    if raw["status"] == COMPLETED:
        _require(
            isinstance(output_sha256, str)
            and _SHA256.fullmatch(output_sha256) is not None,
            f"{where}: output_sha256 inválido",
        )
    else:
        _require(output_sha256 is None, f"{where}: pendiente con hash")

    source = raw["source"]
    _require(
        isinstance(source, dict) and set(source) == _SOURCE_KEYS,
        f"{where}: source inválido",
    )
    _require(
        _is_text(source["kind"])
        and _is_text(source["id"])
        and _is_text(source["name"])
        and _is_int(source["size"])
        and source["size"] >= 0
        and isinstance(source["sha256"], str)
        and _SHA256.fullmatch(source["sha256"]) is not None,
        f"{where}: datos del original inválidos",
    )

    options = raw["options"]
    _require(
        isinstance(options, dict) and set(options) == _OPTION_KEYS,
        f"{where}: options inválido",
    )
    _require(
        _is_int(options["width"])
        and _is_int(options["height"])
        and options["width"] > 0
        and options["height"] > 0
        and options["background"] in BACKGROUNDS,
        f"{where}: opciones inválidas",
    )

    _require(_is_text(raw["model"]), f"{where}: modelo inválido")
    _require(
        _is_text(raw["pipeline_version"]),
        f"{where}: pipeline_version inválido",
    )
    _require(_is_text(raw["recorded_at"]), f"{where}: fecha inválida")

    return ManifestEntry(
        output_name=raw["output_name"],
        status=raw["status"],
        identity=ResultIdentity(
            source_sha256=source["sha256"],
            model=raw["model"],
            width=options["width"],
            height=options["height"],
            background=options["background"],
            pipeline_version=raw["pipeline_version"],
        ),
        source=SourceRecord(
            kind=source["kind"],
            id=source["id"],
            name=source["name"],
            size=source["size"],
            sha256=source["sha256"],
        ),
        output_sha256=output_sha256,
        recorded_at=raw["recorded_at"],
    )


def _entry_to_json(entry: ManifestEntry) -> dict:
    identity = entry.identity

    return {
        "output_name": entry.output_name,
        "status": entry.status,
        "output_sha256": entry.output_sha256,
        "source": {
            "kind": entry.source.kind,
            "id": entry.source.id,
            "name": entry.source.name,
            "size": entry.source.size,
            "sha256": entry.source.sha256,
        },
        "model": identity.model,
        "options": {
            "width": identity.width,
            "height": identity.height,
            "background": identity.background,
        },
        "pipeline_version": identity.pipeline_version,
        "recorded_at": entry.recorded_at,
    }


def _remove_stale_manifest_temps(directory: Path) -> None:
    """Temporales del manifiesto abandonados hace más de 24 horas."""

    now = time.time()

    for path in directory.iterdir():
        if not _MANIFEST_TEMP.fullmatch(path.name):
            continue

        try:
            info = path.lstat()

            if (
                stat.S_ISREG(info.st_mode)
                and now - info.st_mtime >= STALE_TEMP_AGE_SECONDS
            ):
                os.unlink(path)
        except OSError:
            continue


def _replace(tmp: Path, path: Path) -> None:
    # Solo para el manifiesto, que pertenece a la aplicación.
    for delay in PUBLISH_RETRY_DELAYS:
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            sleep(delay)

    os.replace(tmp, path)


class Manifest:
    """Entradas del manifiesto de una carpeta de destino."""

    def __init__(self, directory: Path, entries=()):
        self.directory = Path(directory)
        self.path = self.directory / MANIFEST_NAME
        self._entries: dict[str, ManifestEntry] = {}

        for entry in entries:
            self._entries[name_key(entry.output_name)] = entry

    @classmethod
    def load(cls, directory: Path) -> "Manifest":
        """
        Lee y valida el manifiesto. Si no existe, devuelve uno
        vacío. Si está dañado o es de otra versión, lanza
        ManifestError: nunca lo reinicia.
        """
        directory = Path(directory)
        path = directory / MANIFEST_NAME

        _remove_stale_manifest_temps(directory)

        if not path.exists():
            return cls(directory)

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManifestError(
                f"No se pudo leer el manifiesto {path}: {exc}"
            ) from exc

        _require(isinstance(raw, dict), "no es un objeto JSON")

        version = raw.get("schema_version")

        # 1.0 == 1 en Python: se exige un entero.
        if not _is_int(version) or version != SCHEMA_VERSION:
            raise ManifestError(
                f"Versión de manifiesto no admitida en {path}: "
                f"{version!r}. Esta versión admite {SCHEMA_VERSION}."
            )

        _require(set(raw) == _TOP_LEVEL_KEYS, "campos inesperados")
        _require(raw["generator"] == GENERATOR, "generador desconocido")
        _require(isinstance(raw["entries"], list), "entries no es una lista")

        entries = [
            _parse_entry(item, index)
            for index, item in enumerate(raw["entries"])
        ]

        keys = [name_key(entry.output_name) for entry in entries]
        _require(len(keys) == len(set(keys)), "resultados duplicados")

        return cls(directory, entries)

    def get(self, output_name: str) -> ManifestEntry | None:
        return self._entries.get(name_key(output_name))

    def put(self, entry: ManifestEntry) -> None:
        self._entries[name_key(entry.output_name)] = entry

    def remove(self, output_name: str) -> None:
        self._entries.pop(name_key(output_name), None)

    def entries(self) -> list[ManifestEntry]:
        return sorted(
            self._entries.values(),
            key=lambda entry: name_key(entry.output_name),
        )

    def known_names(self) -> set[str]:
        return set(self._entries)

    def to_json(self) -> str:
        document = {
            "schema_version": SCHEMA_VERSION,
            "generator": GENERATOR,
            "entries": [_entry_to_json(entry) for entry in self.entries()],
        }

        return json.dumps(document, indent=2, ensure_ascii=False) + "\n"

    def save(self) -> None:
        """
        Guarda el manifiesto de forma atómica. Si algo falla,
        el manifiesto anterior queda intacto.
        """
        data = self.to_json().encode("utf-8")

        fd, tmp_name = tempfile.mkstemp(
            dir=self.directory,
            prefix=f"{MANIFEST_NAME}.",
            suffix=TEMP_SUFFIX,
        )
        tmp = Path(tmp_name)

        try:
            with os.fdopen(fd, "wb") as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())

            _replace(tmp, self.path)

        except BaseException as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass

            if isinstance(exc, Exception):
                raise ManifestError(
                    f"No se pudo guardar el manifiesto {self.path}: {exc}"
                ) from exc

            raise
