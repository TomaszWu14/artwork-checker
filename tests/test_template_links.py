"""tests/test_template_links.py — żaden link w szablonie nie może prowadzić w 404.

Nawigacja w tej aplikacji jest budowana z hardkodowanych `href="/..."`, a nie przez
`url_for()`. To znaczy, że literówka albo usunięcie trasy NIE wywala się przy renderze
— użytkownik dostaje ciche 404 i zgłasza to po tygodniu. Ten test odtwarza brakującą
kontrolę: zbiera wszystkie wewnętrzne linki z `templates/` i sprawdza je względem
realnej mapy tras aplikacji.

Wykryte przy pisaniu: /transport/queue i /dostawy/<nr> (moduły usunięte w forku
Artwork), /artwork/reports (literówka, jest /artwork/history) i /tickets (moduł
zgłoszeń nigdy nie istniał).
"""
import os
import re
import tempfile

import pytest

pytest.importorskip("flask")

_TMP = tempfile.mkdtemp(prefix="doccompare_links_")
os.environ.setdefault("SECRET_KEY", "test-links-secret")
os.environ["SQLITE_PATH"] = os.path.join(_TMP, "links.db")
os.environ.pop("DATABASE_URL", None)

import app as _app_mod  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_ROOT, "templates")

# href="/coś" — tylko linki wewnętrzne i statyczne (bez http://, #kotwic, {{ }}).
_HREF_RE = re.compile(r'href="(/[^"{#]*)"')


def _registered_paths():
    """Ścieżki tras jako wzorce regex — trasy z parametrami (`/x/<id>`) dopasowujemy
    po kształcie, bo w szablonie stoi tam konkretna wartość albo wyrażenie Jinja."""
    pats = []
    for rule in _app_mod.app.url_map.iter_rules():
        pats.append(re.compile("^" + re.sub(r"<[^>]+>", r"[^/]+", re.escape(rule.rule)
                                            .replace(r"\<", "<").replace(r"\>", ">")) + "$"))
    return pats


def _template_links():
    out = []
    for name in sorted(os.listdir(_TEMPLATES)):
        if not name.endswith(".html"):
            continue
        path = os.path.join(_TEMPLATES, name)
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                for href in _HREF_RE.findall(line):
                    link = href.split("?")[0].rstrip("/") or "/"
                    if link.startswith("/static"):
                        continue
                    out.append((name, lineno, link))
    return out


def test_every_template_link_hits_a_real_route():
    pats = _registered_paths()
    broken = [(f, ln, link) for f, ln, link in _template_links()
              if not any(p.match(link) for p in pats)]
    assert not broken, "linki prowadzące w 404:\n" + "\n".join(
        f"  {f}:{ln} → {link}" for f, ln, link in broken)


def test_flash_is_rendered_centrally_and_only_once():
    """`flash()` musi być widoczny na KAŻDEJ stronie. get_flashed_messages() konsumuje
    komunikaty, więc dokładnie jeden szablon może je czytać — base.html. Drugi
    czytelnik dostaje pustkę i cicho gubi komunikat (tak było w artwork_admin.html)."""
    readers = []
    for name in sorted(os.listdir(_TEMPLATES)):
        if not name.endswith(".html"):
            continue
        with open(os.path.join(_TEMPLATES, name), encoding="utf-8") as fh:
            body = fh.read()
        # Pomijamy wystąpienia w komentarzu Jinja — to opis, nie odczyt.
        code = re.sub(r"\{#.*?#\}", "", body, flags=re.S)
        if "get_flashed_messages" in code:
            readers.append(name)
    assert readers == ["base.html"], f"flash czytany przez: {readers} (oczekiwano tylko base.html)"


def test_shared_button_styles_are_not_redefined_per_template():
    """`.btn` i jego podstawowe warianty żyją w static/css/app.css, ładowanym przez
    base.html na każdej stronie. Lokalne kopie rozjeżdżały wygląd (cztery kolory
    „primary", cztery promienie) i gubiły :focus-visible, przez co nawigacja Tabem
    stawała się niewidoczna. Warianty własne ekranu (.btn-ai, .btn-green) są OK."""
    shared = ("btn", "btn-primary", "btn-ghost", "btn-outline", "btn-sm")
    offenders = []
    for name in sorted(os.listdir(_TEMPLATES)):
        if not name.endswith(".html"):
            continue
        with open(os.path.join(_TEMPLATES, name), encoding="utf-8") as fh:
            body = fh.read()
        # Strony samodzielne (logowanie, reset hasła, wydruk raportu) nie dziedziczą
        # po base.html, więc app.css do nich nie dociera — tam własny .btn jest
        # konieczny, nie jest duplikatem.
        if 'extends "base.html"' not in body:
            continue
        for block in re.findall(r"<style>(.*?)</style>", body, flags=re.S):
            code = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
            for cls in shared:
                if re.search(r"^\s*\." + re.escape(cls) + r"\s*[,{:]", code, flags=re.M):
                    offenders.append(f"{name}: .{cls}")
    assert not offenders, "style współdzielone przedefiniowane lokalnie:\n  " + "\n  ".join(offenders)


def test_key_pages_are_linked_from_navigation():
    """Funkcje robocze muszą być osiągalne z menu — bez linku istnieją tylko dla kogoś,
    kto zna adres z pamięci. Regresja: /artwork/batch i /library były tak zgubione."""
    with open(os.path.join(_TEMPLATES, "base.html"), encoding="utf-8") as fh:
        nav = fh.read()
    for path in ("/artwork", "/artwork/batch", "/artwork/3d", "/library", "/artwork/kpi"):
        assert f'href="{path}"' in nav, f"{path} zniknęło z nawigacji w base.html"
