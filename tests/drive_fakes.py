"""
Simulación de Google Drive API v3 para las pruebas (sin red).

Solo existen about.get, files.list y files.get: cualquier otra
operación (crear, actualizar, eliminar, copiar, descargar...)
lanza AssertionError.
"""

import copy
import json
import re

import httplib2
from googleapiclient.errors import HttpError


FOLDER_MIME = "application/vnd.google-apps.folder"

ALLOWED = {"files": {"list", "get"}, "about": {"get"}}


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
