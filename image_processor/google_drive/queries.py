"""
Consultas de solo lectura: conexión, carpetas e imágenes.

Solo se usan about.get, files.list y files.get. Las carpetas e
imágenes se identifican por su fileId, nunca solo por su nombre.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..naming import OUTPUT_PREFIX, SUPPORTED_EXTENSIONS
from . import client
from .errors import DriveNotAFolderError, DriveNotFoundError, DriveResponseError


FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

# Formatos que el motor local procesa (ver naming.SUPPORTED_EXTENSIONS).
SUPPORTED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}

PAGE_SIZE = 1000

_FILE_ID = re.compile(r"[A-Za-z0-9_-]+")

_FOLDER_FIELDS = "nextPageToken, files(id, name, parents)"
_IMAGE_FIELDS = (
    "nextPageToken, "
    "files(id, name, mimeType, size, modifiedTime, md5Checksum)"
)

# Carpetas compartidas y unidades compartidas visibles para la cuenta.
_ALL_DRIVES = {"supportsAllDrives": True, "includeItemsFromAllDrives": True}


@dataclass(frozen=True)
class DriveAccount:
    display_name: str | None
    email: str | None


@dataclass(frozen=True)
class DriveFolder:
    id: str
    name: str
    parents: tuple[str, ...] = ()


@dataclass(frozen=True)
class DriveImage:
    id: str
    name: str
    mime_type: str
    size: int | None
    modified_time: str | None
    md5_checksum: str | None


@dataclass(frozen=True)
class SkippedFile:
    id: str | None
    name: str | None
    reason: str


@dataclass
class ImageListing:
    folder: DriveFolder
    images: list[DriveImage] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)


def validate_file_id(file_id: str) -> str:
    """
    Un fileId de Drive solo tiene letras, dígitos, "-" y "_". Se
    valida antes de usarlo en una consulta.
    """
    if not isinstance(file_id, str) or not _FILE_ID.fullmatch(file_id):
        raise ValueError(f"Identificador de Drive inválido: {file_id!r}")

    return file_id


def _executor(execute):
    # Se resuelve en cada llamada: así se puede sustituir client.execute.
    return execute or client.execute


def _pages(make_request, execute):
    """Recorre todas las páginas usando nextPageToken."""

    token = None
    seen = set()

    while True:
        response = execute(make_request(token))
        yield response

        token = response.get("nextPageToken")

        if not token:
            return

        if token in seen:
            raise DriveResponseError(
                "Google Drive repitió un token de paginación."
            )

        seen.add(token)


def verify_connection(service, *, execute=None) -> DriveAccount:
    """Comprueba que la cuenta autorizada puede consultar Drive."""

    response = _executor(execute)(
        service.about().get(fields="user(displayName, emailAddress)")
    )
    user = response.get("user") or {}

    return DriveAccount(
        display_name=user.get("displayName"),
        email=user.get("emailAddress"),
    )


def list_folders(
    service, *, execute=None, page_size: int = PAGE_SIZE
) -> list[DriveFolder]:
    """Todas las carpetas accesibles (no en la papelera)."""

    def make_request(token):
        return service.files().list(
            q=f"mimeType = '{FOLDER_MIME}' and trashed = false",
            fields=_FOLDER_FIELDS,
            orderBy="name",
            pageSize=page_size,
            pageToken=token,
            **_ALL_DRIVES,
        )

    folders = []

    for response in _pages(make_request, _executor(execute)):
        for item in response.get("files") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue

            folders.append(
                DriveFolder(
                    id=item["id"],
                    name=item.get("name") or "",
                    parents=tuple(item.get("parents") or ()),
                )
            )

    return folders


def get_folder(service, folder_id: str, *, execute=None) -> DriveFolder:
    """La carpeta del fileId indicado; falla si no es una carpeta."""

    validate_file_id(folder_id)

    item = _executor(execute)(
        service.files().get(
            fileId=folder_id,
            fields="id, name, mimeType, trashed, parents",
            supportsAllDrives=True,
        )
    )

    if item.get("trashed"):
        raise DriveNotFoundError(f"La carpeta {folder_id} está en la papelera.")

    if item.get("mimeType") != FOLDER_MIME:
        raise DriveNotAFolderError(
            f"El identificador {folder_id} no corresponde a una carpeta."
        )

    return DriveFolder(
        id=item.get("id") or folder_id,
        name=item.get("name") or "",
        parents=tuple(item.get("parents") or ()),
    )


def _parse_size(value) -> int | None:
    if isinstance(value, str) and value.isdigit():
        return int(value)

    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value

    return None


def _classify(item, originals_only: bool):
    """DriveImage si el archivo es admitido; si no, SkippedFile."""

    if not isinstance(item, dict):
        return SkippedFile(None, None, "metadatos incompletos")

    file_id, name, mime = item.get("id"), item.get("name"), item.get("mimeType")

    if not file_id or not name or not mime:
        return SkippedFile(file_id, name, "metadatos incompletos")

    if mime == SHORTCUT_MIME:
        return SkippedFile(file_id, name, "acceso directo (no se sigue)")

    if (
        mime not in SUPPORTED_MIME_TYPES
        or Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS
    ):
        return SkippedFile(file_id, name, "formato no admitido")

    if originals_only and name.upper().startswith(OUTPUT_PREFIX):
        return SkippedFile(file_id, name, "resultado transformado")

    return DriveImage(
        id=file_id,
        name=name,
        mime_type=mime,
        size=_parse_size(item.get("size")),
        modified_time=item.get("modifiedTime"),
        md5_checksum=item.get("md5Checksum"),
    )


def list_images(
    service,
    folder_id: str,
    *,
    originals_only: bool = False,
    execute=None,
    page_size: int = PAGE_SIZE,
) -> ImageListing:
    """
    Imágenes admitidas directamente dentro de la carpeta (sin
    subcarpetas). Un archivo se admite si su tipo MIME y su
    extensión corresponden a un formato del motor local.
    """
    folder = get_folder(service, folder_id, execute=execute)

    def make_request(token):
        return service.files().list(
            q=(
                f"'{folder.id}' in parents and trashed = false "
                f"and mimeType != '{FOLDER_MIME}'"
            ),
            fields=_IMAGE_FIELDS,
            orderBy="name",
            pageSize=page_size,
            pageToken=token,
            **_ALL_DRIVES,
        )

    listing = ImageListing(folder=folder)

    for response in _pages(make_request, _executor(execute)):
        for item in response.get("files") or []:
            result = _classify(item, originals_only)

            if isinstance(result, DriveImage):
                listing.images.append(result)
            else:
                listing.skipped.append(result)

    return listing
