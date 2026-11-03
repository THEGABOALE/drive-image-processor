"""
Errores del motor traducidos para la aplicación web.

Solo mensajes propios: los del motor pueden citar comandos de la CLI, que
aquí no tienen sentido.
"""

from dataclasses import dataclass

from image_processor.background_removal import ModelInitializationError
from image_processor.google_drive.downloads import LocalDiskFullError
from image_processor.google_drive.errors import (
    DriveAccessError,
    DriveAuthError,
    DriveError,
    DriveNotAFolderError,
    DriveNotFoundError,
    DriveQuotaError,
    DriveSecureConnectionError,
    DriveTransientError,
    DriveWriteAccessError,
)
from image_processor.google_drive.local_processing import ForeignFolderError
from image_processor.manifest import ManifestError

from .folders import SourceNotAuthorizedError


RECONNECT_REQUIRED = "reconnect_required"


@dataclass(frozen=True)
class ErrorInfo:
    status: int
    code: str
    message: str


# El orden importa: las subclases van antes.
_ERRORS = (
    (
        SourceNotAuthorizedError, 409, "source_not_authorized",
        "La carpeta de origen no está autorizada para la cuenta conectada. "
        "Elíjala de nuevo con Google Picker usando esa misma cuenta.",
    ),
    (
        DriveWriteAccessError, 409, "target_not_authorized",
        "La cuenta conectada no puede crear archivos en la carpeta de destino. "
        "Elíjala de nuevo con Google Picker usando esa misma cuenta; si es "
        "compartida, necesita permiso de edición.",
    ),
    (
        DriveAuthError, 401, RECONNECT_REQUIRED,
        "La autorización expiró o fue revocada. Vuelva a conectar la cuenta.",
    ),
    (
        DriveNotAFolderError, 422, "not_a_folder",
        "El elemento elegido no es una carpeta.",
    ),
    (
        DriveNotFoundError, 404, "folder_not_found",
        "La carpeta no existe o la cuenta conectada no puede verla.",
    ),
    (
        DriveQuotaError, 503, "quota_exceeded",
        "Se agotó la cuota de Google Drive. Inténtelo de nuevo más tarde.",
    ),
    (
        DriveSecureConnectionError, 502, "secure_connection_error",
        "No se pudo establecer una conexión segura con Google. Revise la red "
        "o el proxy. La verificación no se desactiva.",
    ),
    (
        DriveTransientError, 503, "network_error",
        "No se pudo comunicar con Google Drive. Inténtelo de nuevo.",
    ),
    (
        DriveAccessError, 403, "access_denied",
        "Google Drive denegó el acceso a la carpeta.",
    ),
    (
        DriveError, 502, "drive_error",
        "Google Drive respondió con un error.",
    ),
    (
        LocalDiskFullError, 507, "local_disk_full",
        "No queda espacio en el disco de este equipo. Libere espacio y vuelva a "
        "ejecutar: se retoma sin duplicar.",
    ),
    (
        ModelInitializationError, 500, "model_error",
        "No se pudo preparar el modelo de IA. La primera vez se descarga: revise "
        "la conexión a internet y vuelva a intentarlo.",
    ),
    (
        (ManifestError, ForeignFolderError, NotADirectoryError), 500, "manifest_error",
        "Los registros locales de esta carpeta no se pueden usar. No se modificó "
        "nada; no los borre, porque evitan los duplicados en Drive.",
    ),
)


def describe_error(exc: BaseException) -> ErrorInfo | None:
    """El error para el usuario, o None si no es un error conocido del motor."""

    for kind, status, code, message in _ERRORS:
        if isinstance(exc, kind):
            return ErrorInfo(status, code, message)

    return None
