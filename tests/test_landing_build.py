"""Static releases must contain both languages and only intended public files."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import json
import pytest

from scripts import build_landing


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.ids, self.targets, self.images, self.languages = [], [], [], []
        self.canonicals = []
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
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonicals.append(attrs["href"])


def test_committed_landing_matches_both_locale_sources():
    build_landing.build(check=True)


def test_brand_tokens_are_identical_in_landing_and_dashboard():
    from scripts import build_brand
    build_brand.build(check=True)


@pytest.mark.parametrize("language,relative", [("en", "index.html"), ("ru", "ru/index.html"),
    ("en", "legal/index.html"), ("ru", "ru/legal/index.html"),
    ("en", "404.html"), ("ru", "ru/404.html")])
def test_exported_site_has_resolvable_local_links_and_no_internal_reports(tmp_path, language, relative):
    destination = tmp_path / "site"
    build_landing.export_site(destination)
    actual = {p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()}
    assert len(actual) == 24
    assert {'brand.css', 'product-demo.js', 'product-demo.css',
            'icons/favicon-96x96.png'} <= actual
    assert not any(name.endswith((".md", ".json", ".py", ".env")) for name in actual - {"_routes.json"})
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
            resolved = ((destination / link.path.lstrip('/')) if link.path.startswith('/') else (path.parent / link.path)).resolve()
            assert resolved.is_relative_to(destination)
            assert resolved.is_file() or (resolved / "index.html").is_file(), target
            if link.fragment:
                document = resolved if resolved.is_file() else resolved/'index.html'
                assert link.fragment in Page(document.read_text(encoding='utf-8')).ids


def test_public_license_is_the_current_repository_license():
    assert Path('docs/license.txt').read_text(encoding='utf-8') == Path('LICENSE').read_text(encoding='utf-8')


def test_search_discovery_exports_canonical_pages_and_allows_crawling(tmp_path):
    destination = tmp_path / 'site'
    build_landing.export_site(destination)
    robots = RobotFileParser()
    robots.parse((destination/'robots.txt').read_text(encoding='utf-8').splitlines())
    assert robots.site_maps() == ['https://spooldesk.net/sitemap.xml']
    sitemap = ElementTree.parse(destination/'sitemap.xml')
    ns = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    locations = [item.text for item in sitemap.findall('s:url/s:loc', ns)]
    assert len(locations) == len(set(locations)) == 4
    indexed_files = set()
    for url in locations:
        parsed = urlsplit(url)
        assert parsed.scheme == 'https' and parsed.netloc == 'spooldesk.net'
        assert not parsed.query and not parsed.fragment
        assert robots.can_fetch('Googlebot', url)
        page_path = destination / parsed.path.lstrip('/') / 'index.html'
        assert page_path.resolve().is_relative_to(destination.resolve())
        page = Page(page_path.read_text(encoding='utf-8'))
        assert page.canonicals == [url]
        indexed_files.add(page_path.relative_to(destination).as_posix())
    assert indexed_files == {'index.html', 'ru/index.html', 'legal/index.html', 'ru/legal/index.html'}


def test_export_never_overwrites_an_existing_directory(tmp_path):
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("keep")
    with pytest.raises(FileExistsError):
        build_landing.export_site(destination)
    assert sentinel.read_text() == "keep"


def test_only_event_endpoint_invokes_a_function_and_error_pages_are_not_indexed():
    assert json.loads(Path('docs/_routes.json').read_text()) == {
        'version': 1, 'include': ['/events'], 'exclude': []}
    for name in ('docs/404.html', 'docs/ru/404.html'):
        text = Path(name).read_text(encoding='utf-8')
        assert 'content="noindex, follow"' in text
        assert 'analytics.js' not in text
        assert '>404<' in text
