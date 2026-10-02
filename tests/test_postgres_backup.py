"""Recovery safety before any connection to a real database is attempted."""
import json
from pathlib import Path
import zipfile
import pytest
from scripts import postgres_backup as backup


@pytest.fixture
def bundle(tmp_path):
    store=tmp_path/'files'; key='ab12'+'3'*28
    file=store/key[:2]/key[2:4]/key; file.parent.mkdir(parents=True); file.write_bytes(b'attachment bytes')
    attachments=tmp_path/'attachments.zip'; backup.export_attachments(store,attachments)
    manifest=backup.verify(attachments,attachments_only=True)
    dump=b'PGDMP synthetic dump for unit tests only'
    from io import BytesIO
    manifest['files']['database.dump']=backup.digest(BytesIO(dump))
    manifest['required_attachments']=['attachments/'+file.relative_to(store).as_posix()]
    output=tmp_path/'bundle.zip'
    with zipfile.ZipFile(output,'x') as archive, zipfile.ZipFile(attachments) as source:
        for name in source.namelist():
            if name!='manifest.json': archive.writestr(name,source.read(name))
        archive.writestr('database.dump',dump); archive.writestr('manifest.json',json.dumps(manifest))
    return output,manifest


def test_roundtrip_files_does_not_include_keys_and_never_overwrites(bundle,tmp_path):
    path,manifest=bundle
    assert backup.verify(path)==manifest
    target=tmp_path/'restored'; backup.import_attachments(path,target)
    assert len(list(target.rglob('*'))) == 3
    assert next(p for p in target.rglob('*') if p.is_file()).read_bytes()==b'attachment bytes'
    with pytest.raises(backup.BackupError,match='empty directory'):
        backup.import_attachments(path,target)


@pytest.mark.parametrize('change',['checksum','missing','traversal','duplicate'])
def test_invalid_bundle_refused_before_restore(bundle,tmp_path,change):
    path,manifest=bundle; damaged=tmp_path/'damaged.zip'
    with zipfile.ZipFile(path) as source,zipfile.ZipFile(damaged,'x') as target:
        for name in source.namelist():
            data=source.read(name)
            if change=='missing' and name.startswith('attachments/'): continue
            if change=='checksum' and name=='database.dump': data+=b'corrupted'
            target.writestr(name,data)
        if change=='traversal': target.writestr('../credentials',b'bad')
        if change=='duplicate': target.writestr('database.dump',b'PGDMP')
    with pytest.raises(backup.BackupError): backup.verify(damaged)


def test_restore_requires_offline_empty_database_and_empty_storage(bundle):
    path,_=bundle
    class Target:
        def validate(self): pass
        def running(self): return ['dashboard']
        def sql(self,query): return b'12\n'
        def helper(self,*args,**kwargs): raise AssertionError('must not touch storage')
    with pytest.raises(backup.BackupError,match='offline'): backup.restore(path,offline=False,compose=Target())
    with pytest.raises(backup.BackupError,match='Stop dashboard'): backup.restore(path,offline=True,compose=Target())
    target=Target(); target.running=lambda:[]
    with pytest.raises(backup.BackupError,match='fresh empty'): backup.restore(path,offline=True,compose=target)


def test_failed_backup_restarts_exactly_prior_services_and_publishes_nothing(tmp_path):
    class Target:
        calls=[]
        def validate(self): pass
        def running(self): return ['dashboard']
        def run(self,*args,**kwargs):
            self.calls.append(args)
            if args[0]=='exec': raise backup.BackupError('simulated dump failure')
    target=Target()
    with pytest.raises(backup.BackupError,match='simulated'):
        backup.backup(tmp_path,compose=target)
    assert target.calls[0]==('stop','dashboard') and target.calls[-1]==('start','dashboard')
    assert not list(tmp_path.glob('*.zip')) and not (tmp_path/'.postgres-backup.lock').exists()
