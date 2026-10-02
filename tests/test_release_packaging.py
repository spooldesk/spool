"""Release selection must keep local state and secrets out of source archives."""
from pathlib import Path
import pytest
from scripts.build_release_candidate import allowed, safe_bytes


def test_candidate_allowlist_keeps_source_and_rejects_private_state():
    for name in ['src/dashboard/app.py','tests/frontend/ticket-presence.test.cjs','.env.example',
                 '.github/workflows/ci.yml','requirements.txt','Dockerfile','LICENSE',
                 'docs/icons/site.webmanifest','src/dashboard/static/icons/site.webmanifest']:
        assert allowed(name), name
    for name in ['.env','.env.production','src/.env','data/customer.png','backups/snapshot.zip',
                 '.agent-runs/dump.json','secrets.key','src/nested/secrets.key','MANUAL_STEPS.md',
                 'agents/tasks/private-task.json','../src/example.py','src/test.db','src/__pycache__/test.py']:
        assert not allowed(name), name


def test_candidate_scans_tokens_without_exposing_them(tmp_path):
    (tmp_path/'app.py').write_text('token = "sk-or-v1-'+'x'*40+'"')
    with pytest.raises(ValueError,match='Possible credential in app.py') as err:
        safe_bytes(tmp_path,'app.py')
    assert 'x'*40 not in str(err.value)


def test_candidate_rejects_symlinked_files(tmp_path):
    original=tmp_path/'original.py';original.write_text('print(1)')
    link=tmp_path/'link.py'
    try: link.symlink_to(original)
    except OSError: pytest.skip('Host does not permit creating symlinks')
    with pytest.raises(ValueError,match='Linked path'):
        safe_bytes(tmp_path,'link.py')
