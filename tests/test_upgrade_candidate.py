import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('candidate',Path(__file__).resolve().parents[1]/'scripts/prepare_upgrade_candidate.py')
candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)

class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.root=self.base/'installation';self.root.mkdir()
        self.original=self.base/'original';self.original.mkdir()
        self.git('init',str(self.original))
        self.git('-C',str(self.original),'config','user.name','Local Test')
        self.git('-C',str(self.original),'config','user.email','local@example.invalid')
        (self.original/'README').write_text('first')
        self.git('-C',str(self.original),'add','README');self.git('-C',str(self.original),'commit','-m','first')
        self.sha=self.git('-C',str(self.original),'rev-parse','HEAD')
        (self.original/'README').write_text('second')
        self.git('-C',str(self.original),'commit','-am','second')
        self.latest=self.git('-C',str(self.original),'rev-parse','HEAD')
        self.lock={'repositories':{'qwenpaw':{'remote':str(self.original)}}}
        (self.root/'upstreams.lock.json').write_text(json.dumps(self.lock))
        self.lockbytes=(self.root/'upstreams.lock.json').read_bytes()
        self.patch=patch.dict(candidate.OFFICIAL_REMOTES,{'qwenpaw':str(self.original)})
        self.patch.start();self.addCleanup(self.patch.stop)
    def git(self,*args):
        return subprocess.check_output(['git',*args],stderr=subprocess.DEVNULL,text=True).strip()
    def prepare(self,sha=None): return candidate.prepare(self.root,'qwenpaw',sha or self.sha)
    def target(self): return self.root/'var/upgrade-candidates'/('qwenpaw-'+self.sha)
    def test_isolated_pin_and_idempotence(self):
        first=self.prepare();second=self.prepare()
        self.assertEqual(first['status'],'ready');self.assertTrue(second['reused'])
        self.assertFalse(first['runtimeValidated'])
        self.assertEqual(self.git('-C',str(self.original),'rev-parse','HEAD'),self.latest)
        self.assertEqual((self.original/'README').read_text(),'second')
        self.assertEqual((self.root/'upstreams.lock.json').read_bytes(),self.lockbytes)
        self.assertEqual((self.target()/'README').read_text(),'first')
    def test_dirty_and_wrong_version_are_not_reset(self):
        self.prepare();(self.target()/'README').write_text('user edit')
        with self.assertRaises(candidate.CandidateError): self.prepare()
        self.assertEqual((self.target()/'README').read_text(),'user edit')
        (self.target()/'README').write_text('first')
        self.git('-C',str(self.target()),'checkout','--detach',self.latest)
        with self.assertRaises(candidate.CandidateError): self.prepare()
        self.assertEqual(self.git('-C',str(self.target()),'rev-parse','HEAD'),self.latest)
    def test_wrong_origin_is_refused(self):
        self.prepare();self.git('-C',str(self.target()),'remote','set-url','origin','https://example.invalid/repo')
        with self.assertRaises(candidate.CandidateError): self.prepare()
    def test_failure_does_not_touch_installation(self):
        missing='0'*40
        with self.assertRaises(candidate.CandidateError): self.prepare(missing)
        state=json.loads((self.root/'var/upgrade-candidates'/('qwenpaw-'+missing+'.state.json')).read_text())
        self.assertEqual(state['status'],'failed')
        self.assertEqual(self.git('-C',str(self.original),'rev-parse','HEAD'),self.latest)
        self.assertEqual((self.root/'upstreams.lock.json').read_bytes(),self.lockbytes)
    def test_bad_input_and_remote_rejected_before_clone(self):
        for sha in ['main','a'*39,'a'*40+';echo bad']:
            with self.assertRaises(candidate.CandidateError): self.prepare(sha)
        with self.assertRaises(candidate.CandidateError): candidate.prepare(self.root,'other',self.sha)
        self.lock['repositories']['qwenpaw']['remote']='https://example.invalid/evil'
        (self.root/'upstreams.lock.json').write_text(json.dumps(self.lock))
        with self.assertRaises(candidate.CandidateError): self.prepare()
        self.assertFalse((self.root/'var').exists())
    def test_symlink_and_concurrent_attempt_are_refused(self):
        base=self.root/'var/upgrade-candidates';base.mkdir(parents=True)
        self.target().symlink_to(self.original)
        with self.assertRaises(candidate.CandidateError): self.prepare()
        self.target().unlink()
        (base/('qwenpaw-'+self.sha+'.lock')).write_text('in progress')
        with self.assertRaises(candidate.CandidateError): self.prepare()

    def test_verified_local_cache_clones_independent_objects_and_official_origin(self):
        self.prepare(self.latest)
        calls=[]; original=candidate.git
        def tracked(args,cwd=None):
            calls.append(args)
            return original(args,cwd)
        with patch.object(candidate,'git',side_effect=tracked):
            result=candidate.prepare(self.root,'qwenpaw',self.sha,use_local_cache=True)
        self.assertEqual(result['acquisition'],'verified_local_cache')
        self.assertEqual(result['cacheCommit'],self.latest)
        self.assertFalse(result['runtimeValidated'])
        clone=next(args for args in calls if args[0]=='clone')
        cache=self.root/'var/upgrade-candidates'/('qwenpaw-'+self.latest)
        self.assertEqual(clone[-2],str(cache));self.assertIn('--no-local',clone)
        self.assertEqual(self.git('-C',str(self.target()),'remote','get-url','origin'),str(self.original))
        self.assertFalse((self.target()/'.git/objects/info/alternates').exists())
        inodes={(p.stat().st_dev,p.stat().st_ino) for p in (cache/'.git/objects').rglob('*') if p.is_file()}
        self.assertFalse(any((p.stat().st_dev,p.stat().st_ino) in inodes for p in (self.target()/'.git/objects').rglob('*') if p.is_file()))
        self.assertEqual((self.target()/'README').read_text(),'first')
        replay=candidate.prepare(self.root,'qwenpaw',self.sha,use_local_cache=True)
        self.assertTrue(replay['reused'])
        self.assertEqual(replay['acquisition'],'verified_local_cache')
        self.assertEqual(replay['cacheCommit'],self.latest)

    def test_local_cache_miss_never_clones_remote(self):
        original=candidate.git; calls=[]
        def tracked(args,cwd=None):
            calls.append(args);return original(args,cwd)
        with patch.object(candidate,'git',side_effect=tracked),self.assertRaises(candidate.CandidateError):
            candidate.prepare(self.root,'qwenpaw',self.sha,use_local_cache=True)
        self.assertFalse(any(args[0]=='clone' for args in calls))

    def test_unready_dirty_wrong_remote_and_wrong_path_caches_are_rejected(self):
        self.prepare(self.latest)
        cache=self.root/'var/upgrade-candidates'/('qwenpaw-'+self.latest)
        statepath=cache.with_name(cache.name+'.state.json'); original=json.loads(statepath.read_text())
        for field,value in [('status','failed'),('remote','https://example.invalid/evil'),('path',str(self.original))]:
            statepath.write_text(json.dumps({**original,field:value}))
            with self.subTest(field=field),self.assertRaises(candidate.CandidateError):
                candidate.prepare(self.root,'qwenpaw',self.sha,use_local_cache=True)
        statepath.write_text(json.dumps(original));(cache/'README').write_text('dirty')
        with self.assertRaises(candidate.CandidateError):candidate.prepare(self.root,'qwenpaw',self.sha,use_local_cache=True)
        (cache/'README').write_text('second')
        self.git('-C',str(cache),'remote','set-url','origin','https://example.invalid/evil')
        with self.assertRaises(candidate.CandidateError):candidate.prepare(self.root,'qwenpaw',self.sha,use_local_cache=True)

    def test_cache_missing_requested_commit_fails_without_remote_clone(self):
        self.prepare(self.latest)
        with self.assertRaises(candidate.CandidateError):
            candidate.prepare(self.root,'qwenpaw','0'*40,use_local_cache=True)
        self.assertFalse((self.root/'var/upgrade-candidates'/('qwenpaw-'+'0'*40)).exists())

    def test_symlink_and_alternates_cache_cannot_supply_objects(self):
        self.prepare(self.latest)
        cache=self.root/'var/upgrade-candidates'/('qwenpaw-'+self.latest)
        statepath=cache.with_name(cache.name+'.state.json')
        original=statepath.read_text(); statepath.unlink()
        outside=self.base/'state.json';outside.write_text(original);statepath.symlink_to(outside)
        with self.assertRaises(candidate.CandidateError):candidate.prepare(self.root,'qwenpaw',self.sha,use_local_cache=True)
        statepath.unlink();statepath.write_text(original)
        (cache/'.git/objects/info/alternates').write_text(str(self.original/'.git/objects'))
        with self.assertRaises(candidate.CandidateError):candidate.prepare(self.root,'qwenpaw',self.sha,use_local_cache=True)

if __name__=='__main__': unittest.main()
