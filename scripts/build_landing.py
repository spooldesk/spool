"""Build the English and Russian static landing pages; no Node or network needed."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import shutil
from string import Template

ROOT = Path(__file__).resolve().parents[1]


def write_checked(destination: Path, result: str, check: bool) -> None:
    if check:
        if not destination.exists() or destination.read_text(encoding='utf-8') != result:
            raise ValueError(f'Landing is stale: {destination.relative_to(ROOT)}')
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(result, encoding='utf-8', newline='\n')


def render(template: Template, messages: dict, destination: Path, check: bool) -> None:
    keys = set(template.get_identifiers())
    missing, extra = keys - messages.keys(), messages.keys() - keys
    if missing or extra:
        raise ValueError(f'{destination.name}: missing={sorted(missing)}, unused={sorted(extra)}')
    write_checked(destination, template.substitute({k: html.escape(v, quote=True) for k,v in messages.items()}), check)


def build(*, check: bool = False) -> None:
    # Keep landing and dashboard identity generated from one source.
    if __package__:
        from .build_brand import build as build_brand
    else:
        from build_brand import build as build_brand
    build_brand(check=check)
    source = ROOT / "assets" / "landing"
    template = Template((source / "page.html").read_text(encoding="utf-8"))
    legal_template = Template((source / 'legal.html').read_text(encoding='utf-8'))
    error_template = Template((source / '404.html').read_text(encoding='utf-8'))
    for language, prefix, destination in (
        ("en", "", ROOT / "docs" / "index.html"),
        ("ru", "../", ROOT / "docs" / "ru" / "index.html"),
    ):
        messages = json.loads((source / f"{language}.json").read_text(encoding="utf-8"))
        messages.update(lang=language, prefix=prefix,
                        ai_script_version=hashlib.sha256((ROOT / 'docs/ai-demo.js').read_text(encoding='utf-8').encode()).hexdigest()[:12],
                        ai_style_version=hashlib.sha256((ROOT / 'docs/ai-demo.css').read_text(encoding='utf-8').encode()).hexdigest()[:12],
                        page_path="" if language == "en" else "ru/",
                        other_language_url="ru/" if language == "en" else "../",
                        other_language="Русский" if language == "en" else "English", legal_url='legal/',
                        # The walkthrough is narrated in English: Russian visitors get subtitles on.
                        video_ru_default="" if language == "en" else " default")
        render(template, messages, destination, check)
        legal = json.loads((source / f'{language}-legal.json').read_text(encoding='utf-8'))
        legal.update({key: messages[key] for key in ('lang','skip','theme_label','other_language','other_language_code')})
        legal.update(prefix='../' if language == 'en' else '../../', home_url='../',
            page_path='legal/' if language == 'en' else 'ru/legal/',
            other_language_url='../ru/legal/' if language == 'en' else '../../legal/')
        render(legal_template, legal, destination.parent/'legal/index.html', check)
        error = ({'title': 'Page not found', 'description': 'This address does not exist. Return to Spooldesk to explore the product.', 'back': 'Back to Spooldesk', 'home': '/'}
                 if language == 'en' else {'title': 'Страница не найдена', 'description': 'Такого адреса нет. Вернитесь на Spooldesk, чтобы узнать о продукте.', 'back': 'На главную', 'home': '/ru/'})
        render(error_template, dict(error, lang=language), destination.parent/'404.html', check)
        print(f"{'Checked' if check else 'Built'} {destination.relative_to(ROOT)}")
    write_checked(ROOT/'docs/license.txt', (ROOT/'LICENSE').read_text(encoding='utf-8'), check)
    for name in ('robots.txt', 'sitemap.xml', '_routes.json'):
        write_checked(ROOT/'docs'/name, (source/name).read_text(encoding='utf-8'), check)


def export_site(destination: Path) -> None:
    """Export only public assets, never the internal reports in docs/."""
    paths = ["index.html", "ru/index.html", "legal/index.html", "ru/legal/index.html", "license.txt", "style.css", "landing.js", "brand.css",
             "product-demo.js", "product-demo.css", "ai-demo.js", "ai-demo.css", "robots.txt", "sitemap.xml",
             "404.html", "ru/404.html", "analytics.js", "_routes.json",
             "icons/favicon.ico", "icons/apple-touch-icon.png", "icons/favicon-96x96.png",
             "img/01_board.png", "img/03_ticket_detail.png", "img/16_widget_help.png",
             "video/spooldesk-walkthrough.mp4", "video/spooldesk-walkthrough-poster.jpg",
             "video/spooldesk-walkthrough-en.vtt", "video/spooldesk-walkthrough-ru.vtt"]
    destination.mkdir(parents=True, exist_ok=False)
    for name in paths:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "docs" / name, target)
    # Generated only from disposable fixtures and the real dashboard templates.
    # Explicit extensions: no database, logs, credentials or source reports.
    demo_root = ROOT / 'docs/demo'
    if not (demo_root/'manifest.json').is_file():
        raise ValueError('Build the dashboard demo before exporting the landing')
    for source_file in demo_root.rglob('*'):
        if source_file.is_symlink():
            raise ValueError('Linked files are not allowed in the public demo')
        if source_file.is_file() and source_file.suffix in {'.html','.js','.css','.json','.png','.jpg','.ico','.webmanifest','.woff2','.txt'}:
            target = destination/'demo'/source_file.relative_to(demo_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_file, target)
    print(f"Exported {sum(p.is_file() for p in destination.rglob('*'))} public files to {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if committed pages need rebuilding")
    parser.add_argument("--export", type=Path, help="copy public site assets into a NEW directory")
    arguments = parser.parse_args()
    build(check=arguments.check)
    if arguments.export:
        export_site(arguments.export)
