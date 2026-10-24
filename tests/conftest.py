
import pytest

from image_processor import background_removal


@pytest.fixture(autouse=True)
def forbid_real_models():
    """
    Ninguna prueba puede crear una sesión real de rembg: fallaría
    sin la caché local de modelos o intentaría descargarlos.
    Las pruebas que necesitan una sesión la simulan explícitamente.

    Usa su propia instancia de MonkeyPatch para que un
    monkeypatch.undo() dentro de una prueba no la desactive.
    """

    def forbidden_session(model_name):
        raise AssertionError(
            f"una prueba intentó crear una sesión real de {model_name}"
        )

    guard = pytest.MonkeyPatch()
    background_removal._get_session.cache_clear()
    guard.setattr(background_removal, "new_session", forbidden_session)

    yield

    guard.undo()
    background_removal._get_session.cache_clear()
