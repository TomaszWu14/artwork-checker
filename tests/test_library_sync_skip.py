"""tests/test_library_sync_skip.py — inkrementalny sync nie może czytać całych plików.

Biblioteka to 31k+ PDF-ów na dysku sieciowym. Wcześniej `run_sync` liczył MD5 KAŻDEGO
pliku przed sprawdzeniem, czy cokolwiek się zmieniło — czyli każdy przebieg przeciągał
całą zawartość przez SMB tylko po to, żeby stwierdzić „bez zmian". Te testy pilnują, że
niezmieniony plik jest pomijany po samych metadanych (mtime + rozmiar), a realna zmiana
treści nadal jest wykrywana.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import library_sync as ls


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    """Izolowany „dysk sieciowy" + stan, z podmienionym wysyłaniem na serwer."""
    root = tmp_path / "lib"
    root.mkdir()
    monkeypatch.setattr(ls, "LIBRARY_ROOT", str(root))
    monkeypatch.setattr(ls, "STATE_FILE", str(tmp_path / "state.json"))
    sent = []
    monkeypatch.setattr(ls, "_sync_file",
                        lambda rel, abs_, checksum, dry, send_file=False:
                        (sent.append(rel), "created")[1])
    return root, sent


def _write(root, name, body):
    p = root / name
    p.write_bytes(body)
    return p


def test_unchanged_file_skipped_without_reading_content(sync_env, monkeypatch):
    root, sent = sync_env
    _write(root, "a.pdf", b"%PDF-1.4 tresc")

    ls.run_sync()                                  # pierwszy przebieg — wysyła
    assert sent == ["a.pdf"]

    # Drugi przebieg: MD5 ma prawo NIE zostać policzone. Każde wywołanie = czytanie
    # całego pliku przez sieć, czyli dokładnie ten koszt, który usuwamy.
    def _boom(path):
        raise AssertionError("policzono MD5 niezmienionego pliku — skan czyta dysk")

    monkeypatch.setattr(ls, "_md5", _boom)
    ls.run_sync()
    assert sent == ["a.pdf"], "niezmieniony plik został wysłany ponownie"


def test_changed_content_is_detected(sync_env):
    root, sent = sync_env
    path = _write(root, "a.pdf", b"wersja 1")
    ls.run_sync()
    assert sent == ["a.pdf"]

    # Zmiana treści + inny rozmiar/mtime → musi trafić do wysyłki.
    path.write_bytes(b"wersja 2 - dluzsza")
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 10))
    ls.run_sync()
    assert sent == ["a.pdf", "a.pdf"], "zmieniony plik został pominięty"


def test_touch_without_content_change_does_not_resend(sync_env):
    """Sama zmiana mtime (kopiowanie, backup) nie powinna wywoływać wysyłki —
    metadane kierują do MD5, a ten potwierdza, że treść ta sama."""
    root, sent = sync_env
    path = _write(root, "a.pdf", b"stala tresc")
    ls.run_sync()

    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 500))
    ls.run_sync()
    assert sent == ["a.pdf"], "dotknięcie pliku wywołało niepotrzebną wysyłkę"


def test_failed_sync_is_retried_next_run(sync_env, monkeypatch):
    """Regresja: po błędzie sieci nie wolno zapisać ani checksumu, ani metadanych —
    inaczej plik zostaje wyciszony na zawsze (pominięcie po stamp jest przed MD5)."""
    root, sent = sync_env
    _write(root, "a.pdf", b"tresc")
    monkeypatch.setattr(ls, "_sync_file",
                        lambda rel, abs_, checksum, dry, send_file=False:
                        (sent.append(rel), "error")[1])
    ls.run_sync()
    ls.run_sync()
    assert sent == ["a.pdf", "a.pdf"], "plik po błędzie nie został ponowiony"
