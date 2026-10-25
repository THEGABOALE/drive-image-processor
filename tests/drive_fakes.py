"""
Simulación de Google Drive API v3 para las pruebas (sin red).

Solo existen about.get, files.list, files.get y files.get_media
(descarga del contenido, que nunca modifica Drive): cualquier otra
operación (crear, actualizar, eliminar, copiar, exportar...) lanza
AssertionError.

La descarga se sirve por rangos HTTP a MediaIoBaseDownload real.

FakeDriveServer (GD-03B) modela además la autorización de escritura
drive.file: su vista de escritura solo ve los archivos que creó la
aplicación y las carpetas autorizadas, y solo admite files.create y
files.generateIds como escrituras. Nunca hay update ni delete.
"""

import copy
import hashlib
import json
import re

import httplib2
from googleapiclient.errors import HttpError


FOLDER_MIME = "application/vnd.google-apps.folder"

ALLOWED = {"files": {"list", "get", "get_media"}, "about": {"get"}}
WRITE_ALLOWED = {
    "files": {"list", "get", "create", "generateIds"},
    "about": {"get"},
}


def http_error(status, reasons=(), headers=None):
    response = httplib2.Response({"status": status, **(headers or {})})
    content = json.dumps(
        {
            "error": {
                "code": status,
                "message": "error simulado",
                "errors": [{"reason": reason} for reason in reasons],
            }
        }
    ).encode("utf-8")
    return HttpError(response, content)


class FakeRequest:
    def __init__(self, drive, operation, kwargs):
        self.drive = drive
        self.operation = operation
        self.kwargs = kwargs

    def execute(self):
        self.drive.executions.append(self.operation)

        if self.drive.failures:
            failure = self.drive.failures.pop(0)
            if failure is not None:
                raise failure

        return copy.deepcopy(self.drive.respond(self.operation, self.kwargs))


class FakeHttp:
    """Sirve el contenido por rangos, como Drive con alt=media."""

    def __init__(self, drive, file_id):
        self.drive = drive
        self.file_id = file_id

    def request(self, uri, method="GET", headers=None, **kwargs):
        headers = headers or {}
        self.drive.media_requests.append((self.file_id, headers.get("range")))

        if self.drive.on_media is not None:
            self.drive.on_media(self.file_id, len(self.drive.media_requests))

        if self.drive.media_failures:
            failure = self.drive.media_failures.pop(0)
            if isinstance(failure, BaseException):
                raise failure
            if failure is not None:
                return failure

        content = self.drive.media[self.file_id]
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", headers.get("range", ""))
        start, end = (int(match[1]), int(match[2])) if match else (0, len(content) - 1)
        end = min(end, len(content) - 1)

        response = httplib2.Response({
            "status": "206",
            "content-range": f"bytes {start}-{end}/{len(content)}",
        })
        return response, content[start:end + 1]


class FakeMediaRequest:
    def __init__(self, drive, file_id):
        self.uri = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
        self.headers = {}
        self.http = FakeHttp(drive, file_id)


class FakeResource:
    def __init__(self, drive, kind):
        self._drive = drive
        self._kind = kind

    def __getattr__(self, method):
        if method not in self._drive.allowed[self._kind]:
            raise AssertionError(
                f"operación no permitida: {self._kind}.{method}"
            )

        def call(**kwargs):
            operation = f"{self._kind}.{method}"
            self._drive.calls.append((operation, kwargs))

            if operation == "files.get_media":
                return FakeMediaRequest(self._drive, kwargs["fileId"])

            if operation == "files.create":
                return FakeUploadRequest(self._drive, kwargs)

            return FakeRequest(self._drive, operation, kwargs)

        return call


class FakeDrive:
    allowed = ALLOWED

    def __init__(
        self,
        folders=(),
        children=None,
        items=None,
        about=None,
    ):
        self.folders = list(folders)
        self.children = children or {}
        self.items = items or {}
        self.about_response = about or {
            "user": {"displayName": "Cuenta de prueba", "emailAddress": "prueba@example.com"}
        }
        self.calls = []
        self.executions = []
        # Excepciones que se lanzan, en orden, antes de responder.
        self.failures = []
        # Contenido descargable por fileId y fallos de cada petición
        # HTTP de descarga (None: responder normalmente).
        self.media = {}
        self.media_failures = []
        self.media_requests = []
        # Función opcional llamada antes de servir cada bloque.
        self.on_media = None

        for folder in self.folders:
            self.items.setdefault(
                folder["id"], {"mimeType": FOLDER_MIME, **folder}
            )

    def files(self):
        return FakeResource(self, "files")

    def about(self):
        return FakeResource(self, "about")

    def __getattr__(self, name):
        raise AssertionError(f"recurso no permitido: {name}")

    def respond(self, operation, kwargs):
        if operation == "about.get":
            return self.about_response

        if operation == "files.get":
            item = self.items.get(kwargs["fileId"])

            if item is None:
                raise http_error(404, ["notFound"])

            if isinstance(item, Exception):
                raise item

            return item

        # files.list
        match = re.search(r"'([^']+)' in parents", kwargs["q"])
        files = self.children.get(match.group(1), []) if match else self.folders

        size = kwargs["pageSize"]
        token = kwargs.get("pageToken")
        offset = int(token[1:]) if token else 0

        page = {"files": files[offset:offset + size]}

        if offset + size < len(files):
            page["nextPageToken"] = f"p{offset + size}"

        return page


def image(file_id, name, mime="image/jpeg", **extra):
    item = {
        "id": file_id,
        "name": name,
        "mimeType": mime,
        "size": "1024",
        "modifiedTime": "2026-10-20T12:00:00.000Z",
        "md5Checksum": "0" * 32,
    }
    item.update(extra)
    return item


def folder(file_id, name, parents=("root",)):
    return {"id": file_id, "name": name, "parents": list(parents)}


# GD-03B: escritura con drive.file ---------------------------------------

class FakeUploadRequest:
    """
    files.create con subida reanudable. upload_failures de la vista
    define cada intento: una excepción (antes de llegar a Drive),
    "lost" (Drive crea el archivo y la respuesta se pierde) o None.
    """

    def __init__(self, view, kwargs):
        self.view = view
        self.kwargs = kwargs

    def next_chunk(self, num_retries=0):
        view = self.view
        view.upload_attempts.append(self.kwargs["body"].get("id"))
        failure = view.upload_failures.pop(0) if view.upload_failures else None

        if isinstance(failure, BaseException):
            raise failure

        created = view.server.create(self.kwargs)

        if failure == "lost":
            raise ConnectionResetError("se perdió la respuesta")

        return None, copy.deepcopy(created)


class _View:
    """Una credencial: lectura (drive.readonly) o escritura (drive.file)."""

    def __init__(self, server, role, allowed):
        self.server = server
        self.role = role
        self.allowed = allowed
        self.calls = []
        self.executions = []
        self.failures = []
        self.upload_failures = []
        self.upload_attempts = []

    # La descarga (solo lectura) usa el contenido del servidor.
    @property
    def media(self):
        return self.server.media

    @property
    def media_failures(self):
        return self.server.media_failures

    @property
    def media_requests(self):
        return self.server.media_requests

    @property
    def on_media(self):
        return self.server.on_media

    def files(self):
        return FakeResource(self, "files")

    def about(self):
        return FakeResource(self, "about")

    def __getattr__(self, name):
        raise AssertionError(f"recurso no permitido: {name}")

    def respond(self, operation, kwargs):
        return self.server.respond_as(self.role, operation, kwargs)


class FakeDriveServer(FakeDrive):
    """
    Drive compartido por dos credenciales. app_access son los
    archivos visibles con drive.file: los creados por la aplicación y
    las carpetas autorizadas (por ejemplo, con Picker).
    """

    def __init__(self, *args, app_folders=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.app_access = set(app_folders)
        self.generated = []
        self.created = []
        # fileId -> MD5 que Drive informará (para simular corrupción).
        self.md5_override = {}
        self.read = _View(self, "read", ALLOWED)
        self.write = _View(self, "write", WRITE_ALLOWED)

    def respond_as(self, role, operation, kwargs):
        if operation == "files.generateIds":
            ids = []
            for _ in range(kwargs.get("count", 1)):
                ids.append(f"1Gen{len(self.generated):04d}Id")
                self.generated.append(ids[-1])
            return {"kind": "drive#generatedIds", "space": "drive", "ids": ids}

        if operation == "files.get":
            if role == "write" and kwargs["fileId"] not in self.app_access:
                raise http_error(404, ["notFound"])
            return self.respond(operation, kwargs)

        if operation == "files.list":
            return self._list(role, kwargs)

        return self.respond(operation, kwargs)

    def _list(self, role, kwargs):
        q = kwargs["q"]
        match = re.search(r"'([^']+)' in parents", q)
        files = list(self.children.get(match.group(1), [])) if match else list(self.folders)

        if role == "write":
            files = [item for item in files if item.get("id") in self.app_access]

        if "trashed = false" in q:
            files = [item for item in files if not item.get("trashed")]

        prop = re.search(r"appProperties has \{ key='([^']+)' and value='([^']+)' \}", q)

        if prop:
            files = [
                item for item in files
                if (item.get("appProperties") or {}).get(prop.group(1)) == prop.group(2)
            ]

        size = kwargs["pageSize"]
        token = kwargs.get("pageToken")
        offset = int(token[1:]) if token else 0
        page = {"files": copy.deepcopy(files[offset:offset + size])}

        if offset + size < len(files):
            page["nextPageToken"] = f"p{offset + size}"

        return page

    def create(self, kwargs):
        body = kwargs["body"]
        file_id = body.get("id")

        if file_id not in self.generated:
            raise http_error(400, ["invalid"])

        if file_id in self.items:
            raise http_error(409, ["fileIdInUse"])

        for parent in body.get("parents", []):
            # drive.file: solo carpetas creadas o autorizadas para la app.
            if parent not in self.app_access:
                raise http_error(404, ["notFound"])

        media = kwargs["media_body"]
        assert media.resumable() and media.mimetype() == "image/png"
        data = media.getbytes(0, media.size())

        item = {
            "id": file_id,
            "name": body["name"],
            "mimeType": body["mimeType"],
            "parents": list(body["parents"]),
            "appProperties": dict(body.get("appProperties") or {}),
            "description": body.get("description"),
            "size": str(len(data)),
            "md5Checksum": self.md5_override.get(file_id, hashlib.md5(data).hexdigest()),
            "sha256Checksum": hashlib.sha256(data).hexdigest(),
            "trashed": False,
        }

        self.items[file_id] = item
        for parent in item["parents"]:
            self.children.setdefault(parent, []).append(item)
        self.media[file_id] = data
        self.app_access.add(file_id)
        self.created.append(file_id)
        return item
