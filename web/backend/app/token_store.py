"""
Almacenamiento de la autorización de la aplicación web.

TokenStore es la abstracción: otra implementación (por ejemplo, para una
versión remota) puede reemplazar a la local sin tocar el resto.

La implementación local guarda un único archivo cifrado con DPAPI de
Windows: solo el mismo usuario de Windows en el mismo equipo puede leerlo.
La entropía adicional depende del entorno, así que desarrollo y producción
no pueden leer los datos del otro. Lectura y escritura juntas en un archivo
hacen que las dos autorizaciones se reemplacen a la vez. No hay alternativa
en texto plano: sin DPAPI, la aplicación no guarda tokens.
"""

import ctypes
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Protocol


class TokenStoreError(Exception):
    """El almacenamiento está dañado o no se puede usar. Nunca incluye datos."""


class TokenStore(Protocol):
    def load(self) -> dict | None:
        """Los datos guardados o None. Lanza TokenStoreError si están dañados."""

    def save(self, data: dict) -> None:
        """Reemplaza los datos, de forma atómica."""

    def clear(self) -> bool:
        """Elimina los datos. Devuelve False si no había."""


class EncryptedFileTokenStore:
    """Un archivo cifrado con las funciones protect y unprotect."""

    def __init__(self, path: Path, *, protect, unprotect):
        self._path = Path(path)
        self._protect = protect
        self._unprotect = unprotect

    def load(self) -> dict | None:
        damaged = TokenStoreError(
            "La autorización guardada no se puede leer; hay que volver a conectar."
        )

        try:
            content = self._path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            raise damaged from None

        try:
            data = json.loads(self._unprotect(content))
        except (OSError, ValueError):
            raise damaged from None

        if not isinstance(data, dict):
            raise damaged

        return data

    def save(self, data: dict) -> None:
        content = self._protect(json.dumps(data).encode("utf-8"))
        self._path.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
        )

        try:
            with os.fdopen(fd, "wb") as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())

            os.replace(tmp_name, self._path)

        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def clear(self) -> bool:
        try:
            self._path.unlink()
        except FileNotFoundError:
            return False

        return True


class _Blob(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("data", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob(data: bytes) -> _Blob:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


# Sin diálogos: el servidor local no tiene interfaz propia.
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _call_dpapi(function, data: bytes, entropy: bytes) -> bytes:
    source = _blob(data)
    extra = _blob(entropy)
    output = _Blob()

    if not function(
        ctypes.byref(source), None, ctypes.byref(extra), None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output),
    ):
        # Un OSError con el código de Windows, sin datos.
        raise ctypes.WinError()

    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.windll.kernel32.LocalFree(output.data)


def dpapi_store(data_dir: Path, environment: str) -> EncryptedFileTokenStore:
    if sys.platform != "win32":
        raise TokenStoreError(
            "El almacenamiento protegido de tokens requiere Windows (DPAPI); "
            "no se guardan tokens en texto plano."
        )

    crypt32 = ctypes.windll.crypt32
    entropy = f"DriveImageProcessor/{environment}".encode("utf-8")

    return EncryptedFileTokenStore(
        Path(data_dir) / "auth" / "connection.bin",
        protect=lambda data: _call_dpapi(crypt32.CryptProtectData, data, entropy),
        unprotect=lambda data: _call_dpapi(crypt32.CryptUnprotectData, data, entropy),
    )
