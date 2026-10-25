"""
Simulación de Google Drive API v3 para las pruebas (sin red).

Solo existen about.get, files.list, files.get y files.get_media
(descarga del contenido, que nunca modifica Drive): cualquier otra
operación (crear, actualizar, eliminar, copiar, exportar...) lanza
AssertionError.

La descarga se sirve por rangos HTTP a MediaIoBaseDownload real.
"""

import copy
import json
import re

import httplib2
from googleapiclient.errors import HttpError


FOLDER_MIME = "application/vnd.google-apps.folder"

ALLOWED = {"files": {"list", "get", "get_media"}, "about": {"get"}}


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
        if method not in ALLOWED[self._kind]:
            raise AssertionError(
                f"operación no permitida: {self._kind}.{method}"
            )

        def call(**kwargs):
            operation = f"{self._kind}.{method}"
            self._drive.calls.append((operation, kwargs))

            if operation == "files.get_media":
                return FakeMediaRequest(self._drive, kwargs["fileId"])

            return FakeRequest(self._drive, operation, kwargs)

        return call


class FakeDrive:
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
