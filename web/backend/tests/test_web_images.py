"""
Imágenes para la pantalla: miniaturas del último trabajo y el lienzo de
ejemplo. Sin red ni modelo real (el recorte se simula).
"""

import io

import pytest
from PIL import Image

from web.backend.app import images
from web.backend.app.jobs import FINISHED, JobManager, _account_dir, folder_dirs

SOURCE = "origen"


def png_bytes(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def open_png(content):
    return Image.open(io.BytesIO(content))


def cutout(size=(300, 400)):
    """Un producto opaco en el centro de un lienzo transparente."""
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    image.paste((200, 30, 30, 255), (100, 100, 200, 300))
    return image


@pytest.fixture
def jobs(settings, connected):
    return JobManager(settings, connected, start_thread=lambda target: target())


@pytest.fixture
def api(settings, connected, jobs, make_client):
    return make_client(settings, connected, jobs=jobs)


def dirs(settings):
    downloads, outputs = folder_dirs(settings, "sub-a", SOURCE)
    downloads.mkdir(parents=True, exist_ok=True)
    outputs.mkdir(parents=True, exist_ok=True)
    return downloads, outputs


def finished_job(jobs, files, items=None):
    jobs._sub = "sub-a"
    jobs._job = {
        "id": "a" * 32, "kind": "run", "status": FINISHED,
        "source": {"id": SOURCE, "name": "Originales"},
        "target": {"id": "destino", "name": "Destino"},
        "same_folder": False, "created_at": 1, "finished_at": 2,
        "options": {"width": 400, "height": 400, "background": "white"},
        "progress": {}, "summary": {"items": items or []}, "error": None,
        "files": files,
    }


# Miniaturas del último trabajo ---------------------------------------------

def test_serves_the_original_and_the_result_of_an_item(api, jobs, settings):
    downloads, outputs = dirs(settings)
    Image.new("RGB", (3000, 2000), "navy").save(downloads / "foto--foto1.jpg")
    Image.new("RGB", (400, 400), "white").save(outputs / "TRANSFORMADA_foto.png")
    finished_job(jobs, {"foto1": {"original": "foto--foto1.jpg", "result": "TRANSFORMADA_foto.png"}})

    original = api.get("/api/jobs/current/images/foto1/original")
    result = api.get("/api/jobs/current/images/foto1/result")

    assert original.status_code == 200
    assert original.headers["content-type"] == "image/jpeg"
    assert original.headers["cache-control"] == "no-store"
    assert max(open_png(original.content).size) <= images.THUMBNAIL_MAX_SIDE
    assert result.status_code == 200
    assert result.headers["content-type"] == "image/png"
    assert open_png(result.content).size == (400, 400)


def test_a_transparent_result_keeps_its_transparency(api, jobs, settings):
    _, outputs = dirs(settings)
    cutout().save(outputs / "TRANSFORMADA_foto.png")
    finished_job(jobs, {"foto1": {"original": None, "result": "TRANSFORMADA_foto.png"}})

    image = open_png(api.get("/api/jobs/current/images/foto1/result").content)

    assert image.mode == "RGBA"
    assert image.getpixel((0, 0))[3] == 0


@pytest.mark.parametrize(
    "files",
    [
        {},
        {"foto1": {"original": None, "result": None}},
        {"foto1": {"original": "../../secreto.jpg", "result": None}},
        {"foto1": {"original": "..\\secreto.jpg", "result": None}},
        {"foto1": {"original": ".drive", "result": None}},
        {"foto1": {"original": "no-existe.jpg", "result": None}},
    ],
)
def test_only_files_of_the_job_inside_its_folders_are_served(api, jobs, settings, files):
    downloads, _ = dirs(settings)
    (downloads.parent.parent / "secreto.jpg").write_bytes(png_bytes(Image.new("RGB", (5, 5))))
    finished_job(jobs, files)

    response = api.get("/api/jobs/current/images/foto1/original")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "image_not_available"


def test_hidden_or_temporary_files_are_never_served(api, jobs, settings):
    # Están dentro de la carpeta, pero son temporales o registros del motor.
    downloads, _ = dirs(settings)
    Image.new("RGB", (8, 8)).save(downloads / ".foto.tmp.jpg")
    finished_job(jobs, {"foto1": {"original": ".foto.tmp.jpg", "result": None}})

    assert api.get("/api/jobs/current/images/foto1/original").status_code == 404


def test_another_account_never_sees_the_images_of_a_job(
    settings, jobs, make_manager, make_client
):
    from conftest import read_credentials, write_credentials

    downloads, _ = dirs(settings)
    Image.new("RGB", (64, 64), "navy").save(downloads / "foto--foto1.jpg")
    finished_job(jobs, {"foto1": {"original": "foto--foto1.jpg", "result": None}})
    other = make_manager(read_credentials("sub-b"), write_credentials("sub-b"))
    other.start()
    jobs._connection = other
    client = make_client(settings, other, jobs=jobs)

    assert client.get("/api/jobs/current/images/foto1/original").status_code == 404


@pytest.mark.parametrize("path", ["foto1/otra", "a'b/original", "foto1/original/x"])
def test_invalid_image_requests(api, jobs, path):
    finished_job(jobs, {})

    assert api.get(f"/api/jobs/current/images/{path}").status_code in (404, 422)


def test_damaged_image_is_not_served(api, jobs, settings):
    downloads, _ = dirs(settings)
    (downloads / "rota.jpg").write_bytes(b"no es una imagen")
    finished_job(jobs, {"foto1": {"original": "rota.jpg", "result": None}})

    assert api.get("/api/jobs/current/images/foto1/original").status_code == 404


def test_images_require_a_connection(settings, make_manager, make_client):
    manager = make_manager()
    client = make_client(settings, manager, jobs=JobManager(settings, manager))

    response = client.get("/api/jobs/current/images/foto1/original")

    assert response.status_code == 401


def test_images_of_another_origin_are_rejected(api, jobs):
    finished_job(jobs, {})
    api.headers["Origin"] = "https://evil.example.com"

    assert api.get("/api/jobs/current/images/foto1/original").status_code == 403


def test_the_api_never_exposes_the_local_file_names(api, jobs):
    finished_job(jobs, {"foto1": {"original": "foto--foto1.jpg", "result": "R.png"}})

    body = api.get("/api/jobs/current").json()

    assert "files" not in body["job"]
    assert "foto--foto1.jpg" not in str(body)


# Lienzo de ejemplo --------------------------------------------------------

@pytest.fixture
def fake_model(monkeypatch):
    calls = []

    def remove(image, model_name):
        calls.append(image.size)
        return cutout(image.size)

    monkeypatch.setattr(images.background_removal, "remove_background", remove)
    return calls


def sample(api, width=400, height=400, background="white"):
    return api.get(f"/api/sample?width={width}&height={height}&background={background}")


def test_sample_uses_a_transparent_result_already_processed(api, settings, fake_model):
    _, outputs = dirs(settings)
    cutout().save(outputs / "TRANSFORMADA_foto__400x400_transparent.png")

    response = sample(api)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert open_png(response.content).size == (400, 400)
    assert fake_model == []


def test_sample_follows_the_size_and_the_background(api, settings, fake_model):
    _, outputs = dirs(settings)
    cutout().save(outputs / "TRANSFORMADA_foto.png")

    white = open_png(sample(api, 800, 400, "white").content)
    transparent = open_png(sample(api, 300, 600, "transparent").content)

    assert white.size == (800, 400)
    assert white.mode == "RGB"
    assert white.getpixel((0, 0)) == (255, 255, 255)
    assert transparent.size == (300, 600)
    assert transparent.mode == "RGBA"
    assert transparent.getpixel((0, 0))[3] == 0


def test_sample_places_the_product_like_the_engine(api, settings, fake_model):
    from image_processor.processing import OutputOptions, normalize_image

    _, outputs = dirs(settings)
    cutout().save(outputs / "TRANSFORMADA_foto.png")

    served = open_png(sample(api, 500, 300, "white").content).convert("RGB")
    expected = normalize_image(cutout(), OutputOptions(500, 300, "white"))

    assert served.tobytes() == expected.tobytes()


def test_large_sizes_are_drawn_smaller_with_the_same_proportion(api, settings, fake_model):
    _, outputs = dirs(settings)
    cutout().save(outputs / "TRANSFORMADA_foto.png")

    # 6000 x 2000: dentro del máximo de píxeles del motor.
    image = open_png(sample(api, 6000, 2000, "white").content)

    assert max(image.size) <= images.SAMPLE_MAX_SIDE
    assert abs(image.size[0] / image.size[1] - 3) < 0.01


def test_white_results_are_not_used_as_cutouts(api, settings, fake_model):
    downloads, outputs = dirs(settings)
    Image.new("RGB", (400, 400), "white").save(outputs / "TRANSFORMADA_foto.png")
    Image.new("RGB", (640, 480), "navy").save(downloads / "foto--foto1.jpg")

    assert sample(api).status_code == 200
    assert len(fake_model) == 1


def test_without_results_one_original_is_cut_once_and_kept(api, settings, fake_model):
    downloads, _ = dirs(settings)
    Image.new("RGB", (640, 480), "navy").save(downloads / "foto--foto1.jpg")

    assert sample(api).status_code == 200
    assert sample(api, 800, 600, "transparent").status_code == 200

    assert len(fake_model) == 1
    assert (_account_dir(settings, "sub-a") / "sample" / "cutout.png").exists()


def test_large_originals_are_reduced_before_cutting(api, settings, fake_model):
    downloads, _ = dirs(settings)
    Image.new("RGB", (5000, 4000), "navy").save(downloads / "foto--foto1.jpg")

    sample(api)

    assert max(fake_model[0]) <= images.CUTOUT_MAX_SIDE


def test_metadata_and_temporaries_are_not_used_as_originals(api, settings, fake_model):
    downloads, _ = dirs(settings)
    (downloads / ".drive").mkdir()
    Image.new("RGB", (64, 64), "navy").save(downloads / ".drive" / "x.png")
    Image.new("RGB", (64, 64), "navy").save(downloads / ".tmp-foto.png")
    (downloads / "notas.txt").write_text("hola", encoding="utf-8")

    response = sample(api)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "no_sample"
    assert fake_model == []


def test_no_sample_without_images(api, fake_model):
    response = sample(api)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "no_sample"


def test_empty_cutout_is_not_a_sample(api, settings, monkeypatch):
    downloads, _ = dirs(settings)
    Image.new("RGB", (64, 64), "navy").save(downloads / "foto--foto1.jpg")
    monkeypatch.setattr(
        images.background_removal, "remove_background",
        lambda image, model_name: Image.new("RGBA", image.size, (0, 0, 0, 0)),
    )

    assert sample(api).json()["error"]["code"] == "no_sample"


def test_model_failure_is_reported(api, settings, monkeypatch):
    from image_processor.background_removal import ModelInitializationError

    downloads, _ = dirs(settings)
    Image.new("RGB", (64, 64), "navy").save(downloads / "foto--foto1.jpg")

    def fail(image, model_name):
        raise ModelInitializationError("sin modelo")

    monkeypatch.setattr(images.background_removal, "remove_background", fail)

    response = sample(api)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "model_error"


@pytest.mark.parametrize(
    "query",
    [
        "width=63&height=400&background=white",
        "width=400&height=400&background=azul",
        "width=5000&height=5000&background=white",
        "width=abc&height=400&background=white",
        "height=400&background=white",
    ],
)
def test_sample_validates_the_options(api, settings, query, fake_model):
    _, outputs = dirs(settings)
    cutout().save(outputs / "TRANSFORMADA_foto.png")

    response = api.get(f"/api/sample?{query}")

    assert response.status_code == 422


def test_sample_requires_a_connection(settings, make_manager, make_client):
    manager = make_manager()
    client = make_client(settings, manager)

    assert sample(client).status_code == 401


def test_another_account_never_sees_these_images(settings, fake_model, make_manager, make_client):
    from conftest import read_credentials, write_credentials

    _, outputs = dirs(settings)
    cutout().save(outputs / "TRANSFORMADA_foto.png")
    other = make_manager(read_credentials("sub-b"), write_credentials("sub-b"))
    other.start()
    client = make_client(settings, other, jobs=JobManager(settings, other))

    assert sample(client).status_code == 404


# Original alineado con el resultado ----------------------------------------

PRODUCT = (40, 160, 90)
WALL = (210, 190, 170)


def scene_with_product():
    """Una foto: pared de color y un producto en una zona conocida."""
    image = Image.new("RGB", (1000, 800), WALL)
    image.paste(PRODUCT, (600, 300, 700, 500))
    return image


def product_mask(image, model_name):
    """El "modelo": separa los píxeles del producto (sin red ni onnx)."""
    rgba = image.convert("RGBA")
    alpha = image.convert("RGB").point(lambda value: value)
    mask = Image.new("L", image.size, 0)
    pixels = image.convert("RGB").load()
    out = mask.load()
    for y in range(image.height):
        for x in range(image.width):
            if pixels[x, y] == PRODUCT:
                out[x, y] = 255
    rgba.putalpha(mask)
    return rgba


@pytest.fixture
def aligned_setup(settings, jobs, monkeypatch):
    from image_processor.processing import OutputOptions, normalize_image

    calls = []

    def model(image, model_name):
        calls.append(image.size)
        return product_mask(image, model_name)

    monkeypatch.setattr(images.background_removal, "remove_background", model)
    downloads, outputs = dirs(settings)
    original = scene_with_product()
    original.save(downloads / "foto--f1.png")
    result = normalize_image(product_mask(original, None), OutputOptions(400, 400, "white"))
    result.save(outputs / "TRANSFORMADA_foto.png")
    finished_job(jobs, {"f1": {"original": "foto--f1.png", "result": "TRANSFORMADA_foto.png"}})
    return calls, result


def bbox_of(image, color):
    mask = image.convert("RGB").point(lambda value: value)
    pixels = image.convert("RGB").load()
    xs, ys = [], []
    for y in range(image.height):
        for x in range(image.width):
            r, g, b = pixels[x, y]
            if abs(r - color[0]) < 30 and abs(g - color[1]) < 30 and abs(b - color[2]) < 30:
                xs.append(x)
                ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def test_aligned_original_matches_the_result_framing(api, aligned_setup):
    _, result = aligned_setup

    response = api.get("/api/jobs/current/images/f1/original?aligned=1")

    assert response.status_code == 200
    aligned = open_png(response.content).convert("RGB")
    assert aligned.size == result.size
    expected = bbox_of(result, PRODUCT)
    actual = bbox_of(aligned, PRODUCT)
    assert all(abs(a - b) <= 3 for a, b in zip(actual, expected)), (actual, expected)


def test_aligned_original_keeps_the_photo_around_the_product(api, aligned_setup):
    aligned = open_png(api.get("/api/jobs/current/images/f1/original?aligned=1").content)

    # Junto al producto se ve la pared de la foto, no el fondo del resultado.
    r, g, b = aligned.convert("RGB").getpixel((60, 200))
    assert abs(r - WALL[0]) < 25 and abs(g - WALL[1]) < 25 and abs(b - WALL[2]) < 25


def test_aligned_original_is_cut_once_and_cached(api, aligned_setup, settings):
    calls, _ = aligned_setup

    api.get("/api/jobs/current/images/f1/original?aligned=1")
    api.get("/api/jobs/current/images/f1/original?aligned=1")

    assert len(calls) == 1
    assert any((_account_dir(settings, "sub-a") / "compare").iterdir())


def test_without_aligned_the_plain_original_is_served(api, aligned_setup):
    calls, _ = aligned_setup

    original = open_png(api.get("/api/jobs/current/images/f1/original").content)

    assert original.size == (1000, 800)
    assert calls == []


def test_alignment_falls_back_to_the_plain_original(api, aligned_setup, monkeypatch):
    from image_processor.background_removal import ModelInitializationError

    def fail(image, model_name):
        raise ModelInitializationError("sin modelo")

    monkeypatch.setattr(images.background_removal, "remove_background", fail)

    response = api.get("/api/jobs/current/images/f1/original?aligned=1")

    assert response.status_code == 200
    assert open_png(response.content).size == (1000, 800)


def test_alignment_needs_the_result_too(api, aligned_setup, jobs):
    jobs._job["files"]["f1"]["result"] = None

    response = api.get("/api/jobs/current/images/f1/original?aligned=1")

    assert response.status_code == 200
    assert open_png(response.content).size == (1000, 800)

