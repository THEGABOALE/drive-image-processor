"""
Verificación de las carpetas elegidas en Google Picker.

Además de lo que comprueba el motor (origen legible, destino con escritura
drive.file), el drive.file del backend debe ver también el origen. Es la
comprobación que el navegador no puede falsear: Google concede drive.file
por cuenta y proyecto, así que una carpeta elegida con otra cuenta no es
visible aquí. Nunca se amplían permisos para resolverlo.
"""

from image_processor.google_drive import picker, queries
from image_processor.google_drive.errors import (
    DriveAccessError,
    DriveNotFoundError,
)


class SourceNotAuthorizedError(Exception):
    """El drive.file de la cuenta conectada no ve la carpeta de origen."""


def verify_folders(read_service, write_service, source_id: str, target_id: str):
    """Solo consulta: no crea ni modifica nada en Drive."""

    selection = picker.verify_selection(
        read_service, write_service, source_id, target_id
    )

    if not selection.same_folder:
        try:
            queries.get_folder(write_service, source_id)
        except (DriveNotFoundError, DriveAccessError):
            raise SourceNotAuthorizedError() from None

    return selection
