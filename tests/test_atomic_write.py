
import os
import time

import pytest
from PIL import Image

from image_processor import atomic_write
from image_processor.atomic_write import (
    remove_stale_temp_files,
    save_png_exclusive,
)


PREFIX = "TRANSFORMADA_"
OLD = 25 * 60 * 60


def make_temp(directory, name="TRANSFORMADA_x.png.abc12_yz.tmp", age=OLD):
    path = directory / name
    path.write_bytes(b"parcial")

    mtime = time.time() - age
    os.utime(path, (mtime, mtime))

    return path


def temp_files(directory):
    return list(directory.glob("*.tmp"))


class FailingImage:
    """Escribe datos parciales y luego falla."""

    def __init__(self, error):
        self.error = error

    def save(self, fp, format=None):
        fp.write(b"\x89PNG\r\n\x1a\n parcial")
        raise self.error


def test_save_creates_valid_png_without_temp_files(tmp_path):
    final = tmp_path / "TRANSFORMADA_producto.png"

    save_png_exclusive(
        Image.new("RGB", (400, 400), "red"), final
    )

    with Image.open(final) as image:
        image.load()
        assert image.format == "PNG"
        assert image.size == (400, 400)
        assert image.getpixel((200, 200)) == (255, 0, 0)

    assert temp_files(tmp_path) == []


def test_existing_destination_is_not_overwritten(tmp_path):
    final = tmp_path / "TRANSFORMADA_producto.png"
    final.write_bytes(b"ORIGINAL")

    with pytest.raises(FileExistsError):
        save_png_exclusive(Image.new("RGB", (10, 10)), final)

    assert final.read_bytes() == b"ORIGINAL"
    assert temp_files(tmp_path) == []


@pytest.mark.parametrize(
    "primitive",
    [
        pytest.param(
            "_rename_no_replace",
            marks=pytest.mark.skipif(
                os.name != "nt",
                reason="rename() sobrescribe en POSIX",
            ),
        ),
        "_link_no_replace",
    ],
)
def test_publish_primitive_never_overwrites(tmp_path, primitive):
    tmp = tmp_path / "TRANSFORMADA_x.png.abc12_yz.tmp"
    tmp.write_bytes(b"NUEVO")

    final = tmp_path / "TRANSFORMADA_x.png"
    final.write_bytes(b"ORIGINAL")

    with pytest.raises(FileExistsError):
        getattr(atomic_write, primitive)(tmp, final)

    assert final.read_bytes() == b"ORIGINAL"


def test_destination_created_during_save_is_preserved(tmp_path):
    final = tmp_path / "TRANSFORMADA_producto.png"

    class RacingImage:
        def save(self, fp, format=None):
            fp.write(b"NUEVO")
            # Otro proceso publica primero.
            final.write_bytes(b"AJENO")

    with pytest.raises(FileExistsError):
        save_png_exclusive(RacingImage(), final)

    assert final.read_bytes() == b"AJENO"
    assert temp_files(tmp_path) == []


def test_exception_during_save_leaves_no_files(tmp_path):
    final = tmp_path / "TRANSFORMADA_producto.png"

    with pytest.raises(OSError):
        save_png_exclusive(
            FailingImage(OSError("disco lleno")), final
        )

    assert not final.exists()
    assert temp_files(tmp_path) == []


def test_keyboard_interrupt_during_save_leaves_no_files(tmp_path):
    final = tmp_path / "TRANSFORMADA_producto.png"

    with pytest.raises(KeyboardInterrupt):
        save_png_exclusive(
            FailingImage(KeyboardInterrupt()), final
        )

    assert not final.exists()
    assert temp_files(tmp_path) == []


def test_publish_failure_removes_temp(tmp_path, monkeypatch):
    def failing_publish(tmp, final):
        raise OSError("fallo al publicar")

    monkeypatch.setattr(
        atomic_write, "_publish_once", failing_publish
    )

    final = tmp_path / "TRANSFORMADA_producto.png"

    with pytest.raises(OSError, match="fallo al publicar"):
        save_png_exclusive(Image.new("RGB", (10, 10)), final)

    assert not final.exists()
    assert temp_files(tmp_path) == []


def test_publish_retries_permission_error(tmp_path, monkeypatch):
    real_publish = atomic_write._publish_once
    attempts = []
    sleeps = []

    def flaky_publish(tmp, final):
        attempts.append(tmp)
        if len(attempts) <= 2:
            raise PermissionError("archivo retenido")
        real_publish(tmp, final)

    monkeypatch.setattr(
        atomic_write, "_publish_once", flaky_publish
    )
    monkeypatch.setattr(atomic_write, "sleep", sleeps.append)

    final = tmp_path / "TRANSFORMADA_producto.png"
    save_png_exclusive(Image.new("RGB", (10, 10)), final)

    assert len(attempts) == 3
    assert sleeps == [0.1, 0.2]

    with Image.open(final) as image:
        image.verify()

    assert temp_files(tmp_path) == []


def test_publish_gives_up_after_three_retries(tmp_path, monkeypatch):
    attempts = []
    sleeps = []

    def locked_publish(tmp, final):
        attempts.append(tmp)
        raise PermissionError("archivo retenido")

    monkeypatch.setattr(
        atomic_write, "_publish_once", locked_publish
    )
    monkeypatch.setattr(atomic_write, "sleep", sleeps.append)

    final = tmp_path / "TRANSFORMADA_producto.png"

    with pytest.raises(PermissionError):
        save_png_exclusive(Image.new("RGB", (10, 10)), final)

    assert len(attempts) == 4
    assert sleeps == [0.1, 0.2, 0.4]
    assert not final.exists()
    assert temp_files(tmp_path) == []


def test_file_exists_error_is_not_retried(tmp_path, monkeypatch):
    attempts = []
    sleeps = []

    def existing_publish(tmp, final):
        attempts.append(tmp)
        raise FileExistsError(final)

    monkeypatch.setattr(
        atomic_write, "_publish_once", existing_publish
    )
    monkeypatch.setattr(atomic_write, "sleep", sleeps.append)

    with pytest.raises(FileExistsError):
        save_png_exclusive(
            Image.new("RGB", (10, 10)),
            tmp_path / "TRANSFORMADA_producto.png",
        )

    assert len(attempts) == 1
    assert sleeps == []
    assert temp_files(tmp_path) == []


def test_remove_stale_temp_files_only_matches_pattern(tmp_path):
    stale = make_temp(tmp_path)

    unrelated = [
        make_temp(tmp_path, "notas.tmp"),
        make_temp(tmp_path, "foto.png.abc12_yz.tmp"),
        make_temp(tmp_path, "TRANSFORMADA_x.png.corto.tmp"),
        # Otro nombre base: NTFS no distingue mayúsculas.
        make_temp(tmp_path, "TRANSFORMADA_z.png.ABC12_YZ.tmp"),
        make_temp(tmp_path, "TRANSFORMADA_x.jpg.abc12_yz.tmp"),
        make_temp(tmp_path, "TRANSFORMADA_x.png"),
        make_temp(tmp_path, "foto.jpg"),
    ]

    (tmp_path / "TRANSFORMADA_d.png.abc12_yz.tmp").mkdir()

    removed = remove_stale_temp_files(tmp_path, prefix=PREFIX)

    assert removed == 1
    assert not stale.exists()
    assert all(path.exists() for path in unrelated)
    assert (tmp_path / "TRANSFORMADA_d.png.abc12_yz.tmp").is_dir()


def test_recent_temp_files_are_kept(tmp_path):
    recent = make_temp(tmp_path, age=60 * 60)
    future = make_temp(
        tmp_path,
        "TRANSFORMADA_y.png.abc12_yz.tmp",
        age=-60 * 60,
    )

    assert remove_stale_temp_files(tmp_path, prefix=PREFIX) == 0
    assert recent.exists()
    assert future.exists()


def test_locked_temp_file_is_ignored(tmp_path, monkeypatch):
    make_temp(tmp_path)
    stale = make_temp(tmp_path, "TRANSFORMADA_y.png.abc12_yz.tmp")

    real_unlink = os.unlink

    def locked_unlink(path):
        if str(path).endswith("TRANSFORMADA_x.png.abc12_yz.tmp"):
            raise PermissionError("en uso")
        real_unlink(path)

    monkeypatch.setattr(atomic_write.os, "unlink", locked_unlink)

    assert remove_stale_temp_files(tmp_path, prefix=PREFIX) == 1
    assert (tmp_path / "TRANSFORMADA_x.png.abc12_yz.tmp").exists()
    assert not stale.exists()


@pytest.mark.skipif(os.name != "nt", reason="Bloqueo de archivos de Windows")
def test_windows_open_temp_file_is_not_removed(tmp_path):
    temp = make_temp(tmp_path)

    # Un proceso activo mantiene el archivo abierto.
    with temp.open("rb"):
        assert remove_stale_temp_files(tmp_path, prefix=PREFIX) == 0
        assert temp.exists()


@pytest.mark.skipif(os.name != "nt", reason="NTFS no distingue mayúsculas")
def test_windows_case_insensitive_destination_is_not_overwritten(tmp_path):
    existing = tmp_path / "transformada_producto.png"
    existing.write_bytes(b"ORIGINAL")

    with pytest.raises(FileExistsError):
        save_png_exclusive(
            Image.new("RGB", (10, 10)),
            tmp_path / "TRANSFORMADA_producto.png",
        )

    assert existing.read_bytes() == b"ORIGINAL"
    assert temp_files(tmp_path) == []
