"""
Errores de la integración con Google Drive.

Los mensajes nunca incluyen tokens, credenciales ni el cuerpo de
las respuestas de Google.
"""


class DriveError(RuntimeError):
    """
    Error general de Google Drive. reasons son los motivos que informó
    la API (por ejemplo, "insufficientFilePermissions" o
    "storageQuotaExceeded"), si los hubo.
    """

    reasons: frozenset[str] = frozenset()


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
    """
    Error temporal: de red, de límite de frecuencia o de cuota agotada.
    Los reintentables persistieron después de los reintentos; la cuota
    agotada (DriveQuotaError) y la conexión segura fallida
    (DriveSecureConnectionError) no se reintentan y detienen el lote.
    """


class DriveRateLimitError(DriveTransientError):
    """
    Límite de frecuencia de solicitudes (429, o 403 con
    rateLimitExceeded o userRateLimitExceeded) que persistió después de
    los reintentos. No es una cuota agotada (DriveQuotaError).
    """


class DriveQuotaError(DriveTransientError):
    """
    Se agotó una cuota de la API (403 dailyLimitExceeded o
    quotaExceeded). No es un problema de permisos y no se reintenta
    enseguida: se resuelve más tarde.
    """


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
