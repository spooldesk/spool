"""Static releases must contain both languages and only intended public files."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from scripts import build_landing


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.ids, self.targets, self.images, self.languages = [], [], [], []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag in {"a", "link", "script"}:
            target = attrs.get("href", attrs.get("src", ""))
            if target:
                self.targets.append(target)
        if tag == "img":
            assert attrs.get("alt"), "Product images need descriptive alt text"
            self.images.append(attrs["src"])
        if tag == "html":
            self.languages.append(attrs.get("lang"))


def test_committed_landing_matches_both_locale_sources():
    build_landing.build(check=True)


def test_brand_tokens_are_identical_in_landing_and_dashboard():
    from scripts import build_brand
    build_brand.build(check=True)


@pytest.mark.parametrize("language,relative", [("en", "index.html"), ("ru", "ru/index.html"),
    ("en", "legal/index.html"), ("ru", "ru/legal/index.html")])
def test_exported_site_has_resolvable_local_links_and_no_internal_reports(tmp_path, language, relative):
    destination = tmp_path / "site"
    build_landing.export_site(destination)
    actual = {p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()}
    assert len(actual) == 16
    assert {'brand.css', 'product-demo.js', 'product-demo.css',
            'icons/favicon-96x96.png'} <= actual
    assert not any(name.endswith((".md", ".json", ".py", ".env")) for name in actual)
    path = destination / relative
    page = Page(path.read_text(encoding="utf-8"))
    assert page.languages == [language]
    assert len(page.ids) == len(set(page.ids)), "Duplicate anchor IDs"
    for target in page.targets + page.images:
        if target.startswith(("https://", "mailto:")):
            continue
        if target.startswith("#"):
            assert target == "#" or target[1:] in page.ids
        else:
            link = urlsplit(target)
            resolved = (path.parent / link.path).resolve()
            assert resolved.is_relative_to(destination)
            assert resolved.is_file() or (resolved / "index.html").is_file(), target
            if link.fragment:
                document = resolved if resolved.is_file() else resolved/'index.html'
                assert link.fragment in Page(document.read_text(encoding='utf-8')).ids


def test_public_license_is_the_current_repository_license():
    assert Path('docs/license.txt').read_text(encoding='utf-8') == Path('LICENSE').read_text(encoding='utf-8')


def test_export_never_overwrites_an_existing_directory(tmp_path):
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("keep")
    with pytest.raises(FileExistsError):
        build_landing.export_site(destination)
    assert sentinel.read_text() == "keep"
