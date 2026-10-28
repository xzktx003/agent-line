"""Opt-in cron mounting preserves defaults and rejects changed prepared files."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest

from tests import test_trigger_cron as fixtures


class TriggerCronDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TriggerCronTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.temp.cleanup)
        self.cron = fixtures.cron

    def verify(self):
        return self.cron.verify_prepared(self.fixture.output, self.fixture.original)

    def test_prepared_output_verifies_without_modification(self):
        self.fixture.prepare()
        before = {p.name:p.read_bytes() for p in self.fixture.output.iterdir()}
        self.assertEqual(self.verify(),self.fixture.output)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.fixture.output.iterdir()})

    def test_changed_private_content_mode_extra_or_missing_file_fails(self):
        self.fixture.prepare();file=self.fixture.output/'0.input.json';original=file.read_bytes()
        file.write_bytes(b'{}')
        with self.assertRaises(ValueError):self.verify()
        file.write_bytes(original);file.chmod(0o644)
        with self.assertRaises(ValueError):self.verify()
        file.chmod(0o600);extra=self.fixture.output/'untracked';extra.write_text('no')
        with self.assertRaises(ValueError):self.verify()
        extra.unlink();file.unlink()
        with self.assertRaises(ValueError):self.verify()

    def resign(self,name):
        file=self.fixture.output/'manifest.json';v=json.loads(file.read_text())
        v['files'][name]=hashlib.sha256((self.fixture.output/name).read_bytes()).hexdigest()
        file.write_text(json.dumps(v))

    def test_sender_or_official_jobs_cannot_change_even_with_updated_manifest_hash(self):
        self.fixture.prepare();sender=self.fixture.output/'send.sh';original=sender.read_bytes()
        sender.write_bytes(original+b'echo altered\n');self.resign('send.sh')
        with self.assertRaisesRegex(ValueError,'sender differs'):self.verify()
        sender.write_bytes(original);self.resign('send.sh')
        tab=self.fixture.output/'crontab';tab.write_bytes(tab.read_bytes().replace(b'existing-job',b'different-job'))
        self.resign('crontab')
        with self.assertRaisesRegex(ValueError,'Official cron template changed'):self.verify()

    def test_symlink_and_other_mount_path_are_rejected(self):
        self.fixture.prepare(mount_path='/other-location')
        with self.assertRaisesRegex(ValueError,'fixed managed mount'):self.verify()
        alias=self.fixture.root/'alias';alias.symlink_to(self.fixture.output)
        with self.assertRaises(ValueError):self.cron.verify_prepared(alias,self.fixture.original)

    def wrapper(self, directory=None, *, invalid=False):
        tools=self.fixture.root/'tools';tools.mkdir(exist_ok=True)
        capture=self.fixture.root/'docker-args'
        docker=tools/'docker';docker.write_text('#!/usr/bin/python3\nimport json,os,sys\nopen(os.environ["CAPTURE_ARGS"],"w").write(json.dumps(sys.argv[1:]))\nopen(os.environ["CAPTURE_ARGS"]+"-owner","w").write(os.environ.get("AGENT_LINE_SIM_TRIGGER_CRON_OWNER",""))\n');docker.chmod(0o700)
        python=tools/'python3';python.write_text('#!/bin/sh\ncase "$1" in */trigger_cron.py) exec /usr/bin/python3 "$@";; *) exit 0;; esac\n');python.chmod(0o700)
        env={k:v for k,v in os.environ.items() if k not in ('AGENT_LINE_SIM_TRIGGER_CRON_DIR','AGENT_LINE_SIM_TRIGGER_CRON_OWNER')}
        env.update(PATH=str(tools)+':'+os.environ['PATH'],CAPTURE_ARGS=str(capture))
        if directory is not None:env['AGENT_LINE_SIM_TRIGGER_CRON_DIR']=str(directory)
        command=[str(self.cron.ROOT/'deploy/sim/run.sh'),'config','--quiet']
        result=subprocess.run(command,env=env,cwd=self.fixture.root,capture_output=True)
        return result,json.loads(capture.read_text()) if capture.exists() else None

    def test_default_wrapper_does_not_enable_managed_cron(self):
        result,args=self.wrapper()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(args.count('-f'),1)
        self.assertNotIn(str(self.cron.ROOT/'deploy/sim/triggers.compose.yaml'),args)
        self.assertEqual((self.fixture.root/'docker-args-owner').read_text(),'')

    def test_explicit_verified_absolute_directory_adds_only_optional_override(self):
        self.fixture.original=self.cron.ROOT/'upstream/sim/docker/crontab';self.fixture.prepare()
        result,args=self.wrapper(self.fixture.output)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(args.count('-f'),2)
        self.assertIn(str(self.cron.ROOT/'deploy/sim/triggers.compose.yaml'),args)
        self.assertEqual(args[-2:],['config','--quiet'])
        self.assertEqual((self.fixture.root/'docker-args-owner').read_text(),f'{os.getuid()}:{os.getgid()}')

    def test_invalid_or_relative_opt_in_never_calls_docker(self):
        for directory in (Path('relative'),self.fixture.root/'missing'):
            result,args=self.wrapper(directory)
            self.assertNotEqual(result.returncode,0)
            self.assertIsNone(args)
