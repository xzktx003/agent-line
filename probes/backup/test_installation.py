import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[2] / 'deploy/backup/installation.py'
spec = importlib.util.spec_from_file_location('installation_backup', MODULE)
backup = importlib.util.module_from_spec(spec); spec.loader.exec_module(backup)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
    def tearDown(self): self.temp.cleanup()
    def bundle(self):
        bundle = self.root / 'bundle'; bundle.mkdir()
        dbpath = self.root / 'source.sqlite'
        with sqlite3.connect(dbpath) as db: db.execute('CREATE TABLE admission_guard (id INTEGER PRIMARY KEY, reason TEXT)')
        with tarfile.open(bundle / 'installation.tar', 'w') as archive:
            archive.add(dbpath, arcname='var/adapter/tasks.sqlite')
            data = b'private credential'
            member = tarfile.TarInfo('var/qwenpaw-secrets/token');member.size=len(data)
            archive.addfile(member, io.BytesIO(data))
        (bundle / 'sim-postgres.dump').write_bytes(b'fixture-dump-not-a-live-PG-claim')
        manifest={'formatVersion':1,'sourceRoot':str(self.root/'live'),'members':backup.inventory(bundle/'installation.tar'),
                  'files':{n:{'sha256':backup.sha(bundle/n),'size':(bundle/n).stat().st_size} for n in ('installation.tar','sim-postgres.dump')}}
        (bundle/'manifest.json').write_text(json.dumps(manifest));return bundle
    def test_restore_private_and_admission_closed(self):
        target=self.root/'restored'; backup.restore(self.bundle(),target)
        self.assertEqual(target.stat().st_mode & 0o777,0o700)
        self.assertEqual((target/'var/qwenpaw-secrets/token').stat().st_mode & 0o777,0o600)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o700 for p in target.rglob('*') if p.is_dir()))
        with sqlite3.connect(target/'var/adapter/tasks.sqlite') as db:
            self.assertIn('reconciliation',db.execute('select reason from admission_guard').fetchone()[0])
    def test_corrupt_bundle_refused_before_target_exists(self):
        bundle=self.bundle();(bundle/'installation.tar').write_bytes(b'bad')
        with self.assertRaises(ValueError):backup.restore(bundle,self.root/'restore')
        self.assertFalse((self.root/'restore').exists())
    def test_existing_target_never_overwritten(self):
        bundle=self.bundle();target=self.root/'exists';target.mkdir();(target/'keep').write_text('safe')
        with self.assertRaises(FileExistsError):backup.restore(bundle,target)
        self.assertEqual((target/'keep').read_text(),'safe')
    def test_restore_source_overlap_refused(self):
        with self.assertRaises(ValueError):backup.restore(self.bundle(),self.root/'live'/'restore')
    def test_archive_path_escape_and_links_rejected(self):
        for name,kind in [('../escape',tarfile.REGTYPE),('/absolute',tarfile.REGTYPE),('link',tarfile.SYMTYPE)]:
            path=self.root/'evil.tar'
            with tarfile.open(path,'w') as archive:
                member=tarfile.TarInfo(name);member.type=kind;member.linkname='/etc/passwd';archive.addfile(member)
            with self.assertRaises(ValueError):backup.inventory(path)
    def test_stale_attestation_rejected(self):
        path=self.root/'gate.json';path.write_text(json.dumps({'root':str(self.root),'confirmedNoExternalWriters':True,'issuedAt':time.time()-700}))
        with self.assertRaises(ValueError):backup.verify_quiesce(self.root,path)
    def test_live_process_blocks_before_docker(self):
        path=self.root/'gate.json';path.write_text(json.dumps({'root':str(self.root),'confirmedNoExternalWriters':True,'issuedAt':time.time()}))
        with patch.object(backup,'project_writers',return_value=[123]),patch.object(backup,'run') as command:
            with self.assertRaises(ValueError):backup.verify_quiesce(self.root,path)
            command.assert_not_called()
    def test_postgres_restore_refuses_live_target(self):
        bundle=self.bundle()
        with self.assertRaises(ValueError):backup.restore_postgres(bundle,self.root/'live')
    def test_postgres_count_mismatch_never_claims_success(self):
        bundle=self.bundle();p=bundle/'manifest.json';manifest=json.loads(p.read_text())
        manifest.update(postgresImage='sha256:test-fixture',postgresTableRows={'workflow':2});p.write_text(json.dumps(manifest))
        target=self.root/'restored';backup.restore(bundle,target)
        def fake_run(cmd, **kwargs):
            if '--file=-' in cmd:
                kwargs['stdout'].write(b"SELECT pg_catalog.set_config('search_path', '', false);\n")
        with patch.object(backup,'run',side_effect=fake_run) as command, patch.object(backup.subprocess,'run') as process, patch.object(backup,'postgres_counts',return_value={'workflow':1}):
            process.return_value.returncode=0
            with self.assertRaises(ValueError):backup.restore_postgres(bundle,target)
            launch=command.call_args_list[0].args[0]
            self.assertIn('--network=none',launch)
            self.assertNotIn('-p',launch)
        self.assertFalse((target/'postgres-restore-report.json').exists())
    def test_inventory_tampering_rejected(self):
        bundle=self.bundle();p=bundle/'manifest.json';manifest=json.loads(p.read_text());manifest['members']=[];p.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):backup.validate(bundle)


if __name__=='__main__':unittest.main()
