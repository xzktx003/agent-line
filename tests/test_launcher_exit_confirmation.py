"""No Docker/process signals: private receipts plus fake procfs observations."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'deploy/executor'))
import control
from lifecycle import locked


class LauncherExitTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.path=self.root/'receipt.json'
        self.record={'owner':'a'*32,'containerId':'b'*64,'pid':12345,'start':'101',
                     'uid':os.getuid(),'workspace':str(self.root),'pendingTurns':0,
                     'identities':[{'codexThreadId':'thread','codexTurnId':'turn','nativeTerminal':True}],
                     'exitReceipt':{'owner':'a'*32,'containerId':'b'*64,'source':'docker-inspect-before-remove',
                                    'running':False,'pid':0,'status':'exited','finishedAt':'now'}}
        self.save();mock=patch.object(control,'STATE',self.root);mock.start();self.addCleanup(mock.stop)
    def save(self):
        self.path.write_text(json.dumps(self.record));self.path.chmod(0o600)
    def proof(self):return control.proof('thread','turn',str(self.root))
    def snapshot(self,state='S',start='101'):
        return {'state':state,'start':start,'uid':os.getuid(),'inode':42,'device':1}
    def test_live_and_zombie_do_not_release_even_after_container_exit(self):
        for state in ('S','R','Z'):
            with patch.object(control,'_launcher_snapshot',return_value=self.snapshot(state),create=True):
                result=self.proof();self.assertFalse(result['resourcesReleased']);self.assertIsNone(result['launcherExitProof'])
    def test_absent_and_reused_have_exact_positive_proof(self):
        for snapshot,expected in ((None,'absent'),(self.snapshot(start='202'),'pid_reused')):
            with patch.object(control,'_launcher_snapshot',return_value=snapshot,create=True):
                result=self.proof();self.assertTrue(result['resourcesReleased'])
                proof=result['launcherExitProof'];self.assertEqual(proof['state'],expected)
                self.assertEqual(proof['pid'],12345);self.assertEqual(proof['startTicks'],'101')
                self.assertEqual(set(proof),{'schemaVersion','source','owner','containerId','pid','startTicks','uid','state','observedAt'})
                self.assertEqual(result['exitReceipt'],self.record['exitReceipt'])
                self.assertTrue(self.proof()['resourcesReleased'])
    def test_errors_and_ambiguous_snapshots_remain_unknown(self):
        for error in (PermissionError(),OSError(),ValueError()):
            with patch.object(control,'_launcher_snapshot',side_effect=error,create=True):
                self.assertFalse(self.proof()['resourcesReleased'])
        with patch.object(control,'_launcher_snapshot',side_effect=[self.snapshot(start='202'),self.snapshot(start='203')],create=True):
            self.assertFalse(self.proof()['resourcesReleased'])
    def test_incomplete_or_invalid_original_identity_does_not_release(self):
        original=copy.deepcopy(self.record)
        for key,value in [('start',None),('uid',None),('uid',True),('pid',True),('owner','x'),('containerId','short')]:
            self.record={**original,key:value};self.save()
            with patch.object(control,'_launcher_snapshot',return_value=None,create=True):self.assertFalse(self.proof()['resourcesReleased'])
    def test_container_binding_mismatch_does_not_release(self):
        self.record['exitReceipt']['owner']='c'*32;self.save()
        with patch.object(control,'_launcher_snapshot',return_value=None,create=True):self.assertFalse(self.proof()['resourcesReleased'])
    def test_proc_read_outside_receipt_lock(self):
        def snapshot(pid):
            done=threading.Event()
            def writer():
                with locked(self.path) as record:record['finallyProgress']=True
                done.set()
            thread=threading.Thread(target=writer,daemon=True);thread.start()
            self.assertTrue(done.wait(2),'proc reader holds receipt lock and blocks launcher finally')
            thread.join();return None
        with patch.object(control,'_launcher_snapshot',side_effect=snapshot,create=True):
            self.assertTrue(self.proof()['resourcesReleased'])
        self.assertTrue(json.loads(self.path.read_text())['finallyProgress'])


    def test_proc_reader_missing_directory_distinct_from_missing_stat(self):
        proc=self.root/'proc';proc.mkdir()
        with patch.object(control,'Path',return_value=proc):
            self.assertIsNone(control._launcher_snapshot(12345))
            child=proc/'12345';child.mkdir()
            with self.assertRaises(FileNotFoundError):control._launcher_snapshot(12345)
            fields=['Z']+['0']*18+['101']
            (child/'stat').write_text('12345 (name with ) parentheses) '+' '.join(fields))
            self.assertEqual(control._launcher_snapshot(12345)['state'],'Z')
            (child/'stat').write_text('12345 (short) Z 1')
            with self.assertRaises(ValueError):control._launcher_snapshot(12345)
        with patch.object(control.os,'open',side_effect=PermissionError):
            with self.assertRaises(PermissionError):control._launcher_snapshot(12345)

    def test_receipt_identity_change_during_proc_read_invalidates_proof(self):
        def snapshot(pid):
            with locked(self.path) as record:record['start']='202'
            return None
        with patch.object(control,'_launcher_snapshot',side_effect=snapshot):
            result=self.proof();self.assertFalse(result['resourcesReleased']);self.assertIsNone(result['launcherExitProof'])

    def test_stop_does_not_wait_under_lock_or_signal_reused_pid(self):
        self.record.pop('exitReceipt');self.save()
        def cleanup(record):
            with locked(self.path) as current:
                current['exitReceipt']={'owner':'a'*32,'containerId':'b'*64,'source':'docker-inspect-before-remove',
                                       'running':False,'pid':0,'status':'dead','finishedAt':'now'}
        with patch.object(control,'cleanup',side_effect=cleanup),patch.object(control,'_launcher_snapshot',return_value=self.snapshot(start='202')),patch.object(control.os,'kill') as kill:
            result=control.proof('thread','turn',str(self.root),stop=True)
        self.assertTrue(result['resourcesReleased']);kill.assert_not_called()


if __name__=='__main__':unittest.main()
