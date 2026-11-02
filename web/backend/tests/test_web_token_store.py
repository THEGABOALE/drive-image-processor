import os
import sys

import pytest

from web.backend.app.token_store import (
    EncryptedFileTokenStore,
    TokenStoreError,
    dpapi_store,
)

BUNDLE = {"read_refresh_token": "refresco-muy-secreto", "sub": "sub-a"}


def reversible_protect(data: bytes) -> bytes:
    return bytes(b ^ 0x5A for b in data)


def make_store(tmp_path, **kwargs):
    return EncryptedFileTokenStore(
        tmp_path / "auth" / "connection.bin",
        protect=kwargs.pop("protect", reversible_protect),
        unprotect=kwargs.pop("unprotect", reversible_protect),
    )


def test_empty_store_returns_none(tmp_path):
    assert make_store(tmp_path).load() is None


def test_round_trip_survives_a_new_instance(tmp_path):
    make_store(tmp_path).save(BUNDLE)

    assert make_store(tmp_path).load() == BUNDLE


def test_file_never_contains_the_plain_text(tmp_path):
    make_store(tmp_path).save(BUNDLE)

    content = (tmp_path / "auth" / "connection.bin").read_bytes()
    assert b"refresco-muy-secreto" not in content


def test_save_replaces_atomically_and_leaves_no_temporaries(tmp_path):
    store = make_store(tmp_path)
    store.save(BUNDLE)
    store.save({"sub": "sub-b"})

    assert store.load() == {"sub": "sub-b"}
    assert [p.name for p in (tmp_path / "auth").iterdir()] == ["connection.bin"]


def test_failed_save_keeps_the_previous_connection(tmp_path):
    store = make_store(tmp_path)
    store.save(BUNDLE)

    def broken(data):
        raise OSError("sin disco")

    broken_store = make_store(tmp_path, protect=broken)

    with pytest.raises(OSError):
        broken_store.save({"sub": "sub-b"})

    assert store.load() == BUNDLE
    assert [p.name for p in (tmp_path / "auth").iterdir()] == ["connection.bin"]


def test_clear_removes_the_file(tmp_path):
    store = make_store(tmp_path)
    store.save(BUNDLE)

    assert store.clear() is True
    assert store.load() is None
    assert store.clear() is False


@pytest.mark.parametrize("content", [b"\xff\xfe no es json", b"[1, 2]"])
def test_damaged_store_is_reported_without_its_content(tmp_path, content):
    store = make_store(tmp_path, unprotect=lambda data: data)
    path = tmp_path / "auth" / "connection.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    with pytest.raises(TokenStoreError) as caught:
        store.load()

    assert "no es json" not in str(caught.value)


def test_undecryptable_store_is_reported_as_damaged(tmp_path):
    def cannot_decrypt(data):
        raise OSError("clave de otro usuario")

    store = make_store(tmp_path, unprotect=cannot_decrypt)
    path = tmp_path / "auth" / "connection.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"cifrado")

    with pytest.raises(TokenStoreError):
        store.load()


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI es de Windows")


@windows_only
def test_dpapi_round_trip_and_ciphertext(tmp_path):
    store = dpapi_store(tmp_path, "development")
    store.save(BUNDLE)

    assert store.load() == BUNDLE
    assert b"refresco-muy-secreto" not in (
        tmp_path / "auth" / "connection.bin"
    ).read_bytes()
    assert dpapi_store(tmp_path, "development").load() == BUNDLE


@windows_only
def test_dpapi_data_of_one_environment_cannot_be_read_by_the_other(tmp_path):
    dpapi_store(tmp_path, "development").save(BUNDLE)

    with pytest.raises(TokenStoreError):
        dpapi_store(tmp_path, "production").load()


@windows_only
def test_dpapi_detects_tampering(tmp_path):
    store = dpapi_store(tmp_path, "development")
    store.save(BUNDLE)
    path = tmp_path / "auth" / "connection.bin"
    content = bytearray(path.read_bytes())
    content[-1] ^= 0xFF
    path.write_bytes(bytes(content))

    with pytest.raises(TokenStoreError):
        store.load()


def test_without_dpapi_there_is_no_plain_text_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("web.backend.app.token_store.sys.platform", "linux")

    with pytest.raises(TokenStoreError, match="Windows"):
        dpapi_store(tmp_path, "development")

    assert not os.listdir(tmp_path)
