"""A Docker exit alone cannot admit work onto a closing launcher pipe."""
import copy
import os
import unittest
from tests import test_bridge_resources as fixtures


class LauncherExitBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture=fixtures.ResourcesTests();self.fixture.setUp()
        self.legacy=copy.deepcopy(self.fixture.proof)
        self.legacy.pop('launcherExitProof',None)
        self.proof=copy.deepcopy(self.legacy)
        self.proof['launcherExitProof']=dict(schemaVersion=1,source='procfs-launcher-exit',
            owner=self.proof['owner'],containerId=self.proof['containerId'],pid=123,startTicks='456',
            uid=os.getuid(),state='absent',observedAt=1790000000.25)

    async def test_legacy_container_only_proof_does_not_release(self):
        result=await self.fixture.run_proof(self.legacy)
        self.assertFalse(result['resourcesReleased'])
        self.assertIsNone(result['resourceExitReceipt'])

    async def test_absent_and_reused_launcher_allow_original_docker_proof(self):
        for state in ('absent','pid_reused'):
            self.proof['launcherExitProof']['state']=state
            result=await self.fixture.run_proof(self.proof)
            self.assertTrue(result['resourcesReleased'])
            self.assertEqual(result['resourceExitReceipt'],self.proof['exitReceipt'])

    async def test_incomplete_or_invalid_launcher_evidence_is_not_release(self):
        variants=[None,{},[],{**self.proof['launcherExitProof'],'unexpected':'field'}]
        for key,value in [('schemaVersion',True),('schemaVersion',2),('source','other'),('owner','c'*32),
                ('containerId','d'*64),('uid',os.getuid()+1),('uid',True),('pid',True),('pid',0),('pid',-1),
                ('pid','123'),('startTicks',''),('startTicks','-1'),('startTicks',123),('startTicks','1.2'),
                ('observedAt',float('nan')),('observedAt',float('inf')),('observedAt',0),('observedAt',True),
                ('state','zombie'),('state','running'),('state','unreadable')]:
            variants.append({**self.proof['launcherExitProof'],key:value})
        for variant in variants:
            with self.subTest(variant=variant):
                proof={**self.proof,'launcherExitProof':variant}
                self.assertFalse((await self.fixture.run_proof(proof))['resourcesReleased'])

    async def test_canceled_and_successful_terminal_both_require_launcher_exit(self):
        for status in ('succeeded','canceled'):
            self.fixture.record.update(status=status,cancellationRequested=status=='canceled')
            self.assertFalse((await self.fixture.run_proof(self.legacy))['resourcesReleased'])
            self.assertTrue((await self.fixture.run_proof(self.proof))['resourcesReleased'])
