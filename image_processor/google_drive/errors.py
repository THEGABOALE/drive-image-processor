"""
Errores de la integración con Google Drive.

Los mensajes nunca incluyen tokens, credenciales ni el cuerpo de
las respuestas de Google.
"""


class DriveError(RuntimeError):
    """Error general de Google Drive."""


class DriveAuthError(DriveError):
    """
    Credenciales ausentes, inválidas, expiradas o revocadas.
    Se resuelve volviendo a autorizar; nunca se reintenta solo.
    """


class DriveAccessError(DriveError):
    """La cuenta autorizada no tiene permiso para el recurso (403)."""


class DriveWriteAccessError(DriveAccessError):
    """
    La autorización de escritura (drive.file) no tiene acceso a la
    carpeta de destino: no la creó la aplicación ni fue autorizada.
    """


class DriveNotFoundError(DriveError):
    """El recurso no existe o no es visible para la cuenta (404)."""


class DriveNotAFolderError(DriveError):
    """El identificador existe, pero no corresponde a una carpeta."""


class DriveTransientError(DriveError):
    """Error temporal que persistió después de los reintentos."""


class DriveRateLimitError(DriveTransientError):
    """Google limitó las solicitudes (429 o 403 por cuota)."""


class DriveNetworkError(DriveTransientError):
    """No se pudo comunicar con Google."""


class DriveSecureConnectionError(DriveNetworkError):
    """
    No se pudo establecer la conexión segura (TLS) con Google: no se
    reintenta y la verificación nunca se desactiva.
    """


class DriveCertificateError(DriveSecureConnectionError):
    """Certificado SSL inválido: no se reintenta ni se desactiva."""


class DriveResponseError(DriveError):
    """Respuesta inesperada de la API (por ejemplo, paginación cíclica)."""
