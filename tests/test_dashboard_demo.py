"""The public demo is generated from the real UI, never a customer database."""
import hashlib
import json
import re
from pathlib import Path

from scripts.build_dashboard_demo import clean_page, source_digest

ROOT=Path(__file__).resolve().parents[1]
DEMO=ROOT/'docs/demo'

def test_public_pages_have_no_authentication_material_and_are_guarded():
    manifest=json.loads((DEMO/'manifest.json').read_text(encoding='utf-8'))
    for language, routes in manifest['languages'].items():
        assert {'/tickets','/board','/assistant','/users','/reports','/manage/roles','/manage/integrations'} <= routes.keys()
        assert len(routes)>=140
        for filename in routes.values():
            path=(DEMO/filename).resolve()
            assert path.is_relative_to(DEMO.resolve())
            text=path.read_text(encoding='utf-8')
            assert not re.search(r'<input\b[^>]*name="csrf_token"', text)
            assert not re.search(r'<meta\b[^>]*name="csrf-token"', text)
            assert 'if(!window.SPOOLDEMO)location.replace(' in text
            assert text.count('if(!window.SPOOLDEMO)location.replace(')==1
            assert 'form-action \'none\'' in text
            assert 'name="robots" content="noindex"' in text
            assert 'cdn.jsdelivr.net' not in text
            assert not re.search(r'(?:const CSRF(?:_TOKEN)?\s*=|\bcsrf\s*:)\s*"(?!demo")[^"]+"', text)
    assert len(manifest['rows']['en'])==len(manifest['tickets'])
    for payload in manifest['api'].values():
        for message in payload.get('messages',[]):
            if message.get('attachment_url'):
                assert message['attachment_url'].startswith('/demo/attachments/')
                assert (ROOT/'docs'/message['attachment_url'].lstrip('/')).is_file()

def test_export_tracks_actual_dashboard_sources():
    manifest=json.loads((DEMO/'manifest.json').read_text(encoding='utf-8'))
    for source, digest in manifest['sources'].items():
        assert source_digest(ROOT/source)==digest, f'Rebuild dashboard demo after editing {source}'
    for path in (ROOT/'src/dashboard/static').rglob('*'):
        if path.is_file() and path.suffix in {'.css','.js'}:
            assert (DEMO/'static'/path.relative_to(ROOT/'src/dashboard/static')).read_text(encoding='utf-8')==path.read_text(encoding='utf-8').replace('/static/','/demo/static/')

def test_csrf_scrubbing_and_navigation_keep_real_markup():
    source='<meta name="csrf-token" content="SECRET"><input type="hidden" name="csrf_token" value="SECRET"><script>const CSRF_TOKEN = "SECRET"; const CSRF="SECRET"; boot({csrf:"SECRET"});</script><a href="/tickets">Tickets</a>'
    assert 'SECRET' not in clean_page(source)
    assert '<a href="/tickets">Tickets</a>' in clean_page(source)

def test_demo_frame_blocks_network_and_native_form_submissions():
    shell=(DEMO/'shell.js').read_text(encoding='utf-8')
    assert "connect-src 'none'" in shell and "form-action 'none'" in shell
    assert 'event.source!==frame.contentWindow' in shell
    assert 'event.origin!==location.origin' in shell
    index=(DEMO/'index.html').read_text(encoding='utf-8')
    assert 'allow-top-navigation' not in index
    assert 'allow-popups' not in index
