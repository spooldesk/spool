"""Export the actual dashboard templates with disposable, fictional fixtures.

No deployment database, .env or credentials are read. This is a build tool,
not a public application server. The static demo never calls the app API.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DEST = ROOT / "docs/demo"
STATIC = ROOT / "src/dashboard/static"
PAGES = (
    "/tickets", "/tickets?status=closed", "/board", "/users", "/assistant",
    "/overview", "/reports", "/ai", "/templates", "/manage/templates",
    "/manage/templates/new", "/manage/articles", "/manage/articles/new",
    "/manage/categories", "/manage/close-reasons", "/manage/teams",
    "/manage/settings", "/manage/integrations", "/manage/admins", "/manage/roles",
    "/manage/ai", "/me", "/help",
)


def block_network(*args, **kwargs):
    raise RuntimeError("Network access is disabled in the dashboard demo builder")


async def block_async_network(*args, **kwargs):
    return block_network()


def prepare_fixture(path: Path) -> None:
    from scripts.demo_environment import configure, pin_offline_settings
    from scripts import seed_demo_data
    from datetime import datetime
    os.environ["TESTING"] = "1"
    configure(path)
    # Also remove providers not covered by the older screenshot launcher.
    for key in tuple(os.environ):
        if key.startswith(("OPENROUTER_", "AZURE_", "AWS_")):
            os.environ.pop(key, None)
    seed_demo_data.NOW = datetime(2026, 10, 17, 10, 0)
    asyncio.run(seed_demo_data.main(path))
    pin_offline_settings(path)
    with closing(sqlite3.connect(path)) as db:
        for key, value in {"ai_enabled":"1", "ai_assistant_enabled":"1",
                           "ai_model":"Demo · no model connected", "brand_name":"Spooldesk"}.items():
            db.execute("INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,datetime('now')) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key,value))
        db.execute("UPDATE admin_users SET name='Alex Morgan',email='alex@example.test' WHERE role='owner'")
        db.execute("UPDATE admin_users SET preferences=?", (json.dumps({"language":"en","theme":"dark","hotkeys":True}),))
        # All addresses are reserved fixture addresses; no configured adapters.
        for uid, username in db.execute("SELECT id,username FROM users WHERE channel='telegram'").fetchall():
            channel = ("email", "slack", "telegram", "widget")[(uid-1)%4]
            address = f"customer-{uid}@example.test" if channel == "email" else f"demo-{uid}"
            db.execute("UPDATE users SET channel=?,channel_user_id=?,email=?,username=? WHERE id=?",
                       (channel,address,f"customer-{uid}@example.test",(username or "Customer").replace('_',' ').title(),uid))
        db.commit()


def clean_page(source: str) -> str:
    # No authentication material is ever part of the generated public files.
    source = re.sub(r'<meta\s+name="csrf-token"[^>]*>', '', source, flags=re.I)
    source = re.sub(r'<input\b[^>]*name="csrf_token"[^>]*>', '', source, flags=re.I)
    source = re.sub(r'(const CSRF(?:_TOKEN)?\s*=\s*)[^;]+;', r'\1"demo";', source)
    source = re.sub(r'((?:"csrf"|\bcsrf)\s*:\s*)"[^"]*"', r'\1"demo"', source)
    source = re.sub(r'(/static/[^"\s?<>]+)\?v=[a-f0-9]+', r'\1', source)
    source = source.replace('/static/', '/demo/static/')
    source = source.replace('https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.19.0/dist/tabler-icons.min.css', '/demo/vendor/tabler-icons.min.css')
    source = source.replace('https://cdn.jsdelivr.net/npm/chart.js@4.4.6/dist/chart.umd.min.js', '/demo/vendor/chart.umd.min.js')
    source = source.replace("'dash-", "'spooldesk-demo-")
    # Existing app handlers that navigate use the demo router in this export.
    source = re.sub(r"(?:window\.)?location\s*=\s*(['\"])(/[^'\"]*)\1", r"window.demoNavigate('\2')", source)
    source = re.sub(r'(?:window\.)?location\.href\s*=\s*([^;\n]+)', r'window.demoNavigate(\1)', source)
    return source


def source_digest(path: Path) -> str:
    # Git normalizes text line endings differently on Windows and Linux.
    data = (path.read_text(encoding='utf-8').encode('utf-8')
            if path.suffix in {'.html','.js','.css','.json','.webmanifest','.svg','.txt'}
            else path.read_bytes())
    return hashlib.sha256(data).hexdigest()


def source_manifest() -> dict:
    paths = list((ROOT/'src/dashboard/templates').glob('*.html')) + list(STATIC.rglob('*'))
    return {p.relative_to(ROOT).as_posix(): source_digest(p)
            for p in sorted(paths) if p.is_file()}


def build(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="spooldesk-public-demo-",ignore_cleanup_errors=True) as temp:
        database = Path(temp)/"fixture.db"
        prepare_fixture(database)
        from fastapi.testclient import TestClient
        from src.dashboard.app import app
        from src.dashboard.routes import tickets as ticket_routes
        # Keep every fictional row searchable in the browser; production paging is unchanged.
        ticket_routes.PAGE_SIZE = 100
        from src.dashboard.auth import SESSION_COOKIE, make_session_cookie
        import aiohttp, httpx, smtplib, imaplib
        with closing(sqlite3.connect(database)) as db:
            owner = db.execute("SELECT id,session_version FROM admin_users WHERE role='owner'").fetchone()
            ticket_ids = [r[0] for r in db.execute("SELECT id FROM tickets ORDER BY id")]
            users = [r[0] for r in db.execute("SELECT id FROM users")]
            articles = list(db.execute("SELECT id,slug,is_published FROM articles"))
            templates = [r[0] for r in db.execute("SELECT id FROM reply_templates")]
            tickets = [dict(zip(('id','status','stage','assigned_admin_id','category','team_id','user_id'),r)) for r in db.execute("SELECT id,status,stage,assigned_admin_id,category,team_id,user_id FROM tickets")]
        routes = list(PAGES) + [f'/tickets/{i}' for i in ticket_ids] + [f'/users/{i}' for i in users]
        routes += [f'/manage/articles/{i}/edit' for i,_,_ in articles]
        routes += [f'/help/{slug}' for _,slug,live in articles if live]
        routes += [f'/manage/templates/{i}/edit' for i in templates]
        manifest = {"version":1,"languages":{},"tickets":tickets,"rows":{},"sources":source_manifest()}
        with patch.object(httpx.AsyncHTTPTransport,'handle_async_request',block_async_network), \
             patch.object(httpx.HTTPTransport,'handle_request',block_network), \
             patch.object(aiohttp.TCPConnector,'_create_connection',block_async_network), \
             patch.object(smtplib.SMTP,'connect',block_network), \
             patch.object(imaplib.IMAP4,'open',block_network), TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE,make_session_cookie(*owner))
            from src.services import preferences_service, settings_service
            from src.dashboard import i18n
            for lang in ('en','ru'):
                client.portal.call(preferences_service.save,owner[0],{'language':lang,'theme':'dark','hotkeys':True})
                client.portal.call(settings_service.set_dash_language,lang)
                i18n.set_active_language(lang)
                app.state.templates.env.globals['T'] = i18n.strings(lang)
                page_dir = destination/lang
                page_dir.mkdir(exist_ok=True)
                mapping={}
                manifest['rows'][lang]={}
                for route in routes:
                    response=client.get(route)
                    if response.status_code != 200 or 'text/html' not in response.headers.get('content-type',''):
                        raise ValueError(f'Cannot export {route}: {response.status_code}')
                    name=hashlib.sha256(route.encode()).hexdigest()[:16]+'.html'
                    cleaned = clean_page(response.text)
                    guard = ('<meta name="robots" content="noindex">'
                             '<meta http-equiv="Content-Security-Policy" content="connect-src \'none\'; form-action \'none\'; object-src \'none\'">'
                             '<script>if(!window.SPOOLDEMO)location.replace(' + json.dumps('/demo/?lang='+lang+'#'+route).replace('<','\\u003c') + ');</script>')
                    cleaned = cleaned.replace('<head>', '<head>'+guard, 1)
                    (page_dir/name).write_text(cleaned,encoding='utf-8',newline='\n')
                    if route in ('/tickets','/tickets?status=closed'):
                        for row in re.findall(r'<tr\b[^>]*onclick="[^"]+"[^>]*>.*?</tr>',cleaned,re.S):
                            ident=re.search(r'/tickets/(\d+)',row)
                            if ident: manifest['rows'][lang][ident[1]]=row
                    mapping[route]=f'{lang}/{name}'
                manifest['languages'][lang]=mapping
                print(f'Exported {len(mapping)} authentic {lang} pages')
            # Allowlisted public API fixtures, no broad database/settings dump.
            api={}
            for tid in ticket_ids:
                for suffix in ('messages.json','templates.json'):
                    route=f'/tickets/{tid}/{suffix}'
                    response=client.get(route)
                    if response.status_code!=200: raise ValueError(route)
                    api[route]=response.json()
            for route in ('/api/queue-status','/api/tickets-version'):
                api[route]=client.get(route).json()
            # Copy only locally generated fixture attachments referenced by messages.
            for payload in api.values():
                for message in payload.get('messages',[]):
                    url=message.get('attachment_url')
                    if url and url.startswith('/tickets/attachment/'):
                        response=client.get(url)
                        if response.status_code==200:
                            attachment=destination/'attachments'/f'{message["id"]}.png'
                            attachment.parent.mkdir(exist_ok=True)
                            attachment.write_bytes(response.content)
                            message['attachment_url']=f'/demo/attachments/{message["id"]}.png'
            manifest['api']=api
        for asset in STATIC.rglob('*'):
            if asset.is_file() and asset.suffix in {'.css','.js','.png','.ico','.webmanifest'}:
                target=destination/'static'/asset.relative_to(STATIC)
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(asset.read_bytes().replace(b'/static/',b'/demo/static/') if asset.suffix in {'.js','.css'} else asset.read_bytes())
        (destination/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
        print(f'Public demo built: {destination}')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=DEST)
    args=parser.parse_args()
    build(args.output.resolve())
