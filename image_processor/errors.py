"""Errores del pipeline de procesamiento."""


class EmptySegmentationError(RuntimeError):
    """
    El modelo no detectó ningún objeto: todo el canal alfa
    de la segmentación es cero.

    Es un fallo de una fotografía concreta, distinto de
    ModelInitializationError, que impide procesar el lote.
    """
