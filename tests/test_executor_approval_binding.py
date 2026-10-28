"""Private temporary launch receipts and fake Docker only; no real processes."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'deploy/executor'))
import launcher
import control

METHOD = 'item/commandExecution/requestApproval'


class ApprovalBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root/'owner.json'
        self.record = dict(workspace=str(self.root), owner='a'*32, containerId='b'*64,
                           identities=[dict(codexThreadId='thread', codexTurnId='turn', nativeTerminal=False)],
                           pendingTurns=0, accepting=True)
        self.save()
        self.state = patch.object(control, 'STATE', self.root); self.state.start(); self.addCleanup(self.state.stop)
        self.docker = patch.object(control, 'inspect_owned', return_value={'Id':'b'*64,'State':{'Running':True,'Pid':123}})
        self.docker.start(); self.addCleanup(self.docker.stop)

    def save(self):
        self.path.write_text(json.dumps(self.record)); self.path.chmod(0o600)

    def message(self, **params):
        return {'id':7,'method':METHOD,'params':dict(threadId='thread',turnId='turn',itemId='item',command='SECRET',**params)}

    def bind(self, message=None):
        launcher.record_approval(self.record, message or self.message()); self.save()

    def get(self):
        return control.approvals('thread','turn',str(self.root))

    def test_binding_exact_and_no_command(self):
        self.bind(); self.bind()
        value = self.get()
        self.assertEqual(value['approvals'], [dict(requestId=7,method=METHOD,threadId='thread',turnId='turn',itemId='item')])
        self.assertNotIn('SECRET', json.dumps(value)); self.assertNotIn('SECRET', self.path.read_text())
        self.assertEqual(len(self.record['approvalBindings']), 1)

    def test_missing_identity_and_overflow_fail_closed(self):
        message=self.message(); del message['params']['turnId']; self.bind(message)
        with self.assertRaises(RuntimeError): self.get()
        self.record.pop('approvalBindingsUnavailable', None); self.record['approvalBindings']=[]
        for i in range(257):
            message=self.message(); message['id']=i; message['params']['itemId']='item-'+str(i)
            launcher.record_approval(self.record, message)
        self.save()
        self.assertLessEqual(len(self.record['approvalBindings']), 256)
        with self.assertRaises(RuntimeError): self.get()

    def test_wrong_turn_and_multiple_active_turns_rejected(self):
        message=self.message(); message['params']['turnId']='wrong'; self.bind(message)
        with self.assertRaises(RuntimeError): self.get()
        self.record['approvalBindings']=[]; self.bind()
        self.record['identities'].append(dict(codexThreadId='thread',codexTurnId='other',nativeTerminal=False)); self.save()
        with self.assertRaises(RuntimeError): self.get()

    def test_duplicate_item_ambiguous_and_resolved_hidden(self):
        self.bind(); message=self.message(); message['id']=8; self.bind(message)
        with self.assertRaises(RuntimeError): self.get()
        self.record.pop('approvalBindingsUnavailable',None); self.record['approvalBindings']=[]; self.bind()
        launcher.resolve_approval(self.record,7,thread_id='thread'); self.save()
        self.assertEqual(self.get()['approvals'], [])

    def test_not_live_or_pending_or_exited_denied(self):
        self.bind()
        for field,value in [('pendingTurns',1),('exitReceipt',{'proof':True}),('accepting',False)]:
            old=self.record.get(field); self.record[field]=value; self.save()
            with self.assertRaises(RuntimeError): self.get()
            if old is None:self.record.pop(field)
            else:self.record[field]=old
        self.save()
        with patch.object(control,'inspect_owned',return_value={'Id':'b'*64,'State':{'Running':False,'Pid':0}}):
            with self.assertRaises(RuntimeError): self.get()

    def test_proxy_persists_before_forward_and_server_id_does_not_consume_client_pending(self):
        import threading
        ready=threading.Event(); parent_output=io.BytesIO(); receipt=self.path
        class Input(io.BytesIO):
            def write(self, data):
                result=super().write(data); ready.set(); return result
            def close(self): pass
        request={'id':7,'method':'turn/start','params':{'threadId':'thread'}}
        approval=self.message()
        class ChildOutput:
            def __iter__(self):
                assert ready.wait(2)
                yield (json.dumps(approval)+'\n').encode()
                yield (json.dumps({'id':7,'result':{'turn':{'id':'turn'}}})+'\n').encode()
        class Output:
            def write(self, data):
                if json.loads(data).get('method')==METHOD:
                    saved=json.loads(receipt.read_text())
                    assert saved['approvalBindings'][0]['itemId']=='item'
                parent_output.write(data)
            def flush(self): pass
        child=type('Child',(),{'stdin':Input(),'stdout':ChildOutput(),'wait':lambda self:0})()
        with patch.object(launcher.sys,'stdin',type('Stream',(),{'buffer':io.BytesIO((json.dumps(request)+'\n').encode())})()), patch.object(launcher.sys,'stdout',type('Stream',(),{'buffer':Output()})()):
            self.assertEqual(launcher.proxy(child,self.path),0)
        saved=json.loads(self.path.read_text())
        self.assertEqual(saved['pendingTurns'],0)
        self.assertEqual(saved['identities'][-1]['codexTurnId'],'turn')
        self.assertFalse(saved['approvalBindings'][0].get('resolved',False))

    def test_three_methods_and_typed_ids_resolve_only_exact_request(self):
        for i, method in enumerate(sorted(launcher.APPROVAL_METHODS)):
            message=self.message(); message['method']=method; message['id']=str(i)
            message['params']['itemId']='item-'+str(i); self.bind(message)
        self.assertEqual(len(self.get()['approvals']),3)
        launcher.resolve_approval(self.record, 0, thread_id='thread'); self.save()
        self.assertEqual(len(self.get()['approvals']),3)
        launcher.resolve_approval(self.record, '0', thread_id='other'); self.save()
        self.assertEqual(len(self.get()['approvals']),3)
        launcher.resolve_approval(self.record, '0', thread_id='thread'); self.save()
        self.assertEqual(len(self.get()['approvals']),2)

    def test_request_id_reuse_with_changed_identity_fail_closed(self):
        self.bind(); message=self.message(); message['params']['itemId']='different'; self.bind(message)
        with self.assertRaises(RuntimeError):self.get()

    def test_resolved_notification_proxy(self):
        self.bind()
        messages=[{'method':'serverRequest/resolved','params':{'threadId':'thread','requestId':7}}]
        child=type('Child',(),{'stdin':io.BytesIO(),'stdout':[(json.dumps(m)+'\n').encode() for m in messages],'wait':lambda self:0})()
        output=io.BytesIO()
        with patch.object(launcher.sys,'stdin',type('Stream',(),{'buffer':io.BytesIO()})()), patch.object(launcher.sys,'stdout',type('Stream',(),{'buffer':output})()):
            launcher.proxy(child,self.path)
        self.assertEqual(self.get()['approvals'],[])

    def test_response_direction_marks_resolved_before_forward(self):
        import threading
        self.bind(); forwarded=threading.Event(); path=self.path
        class Input:
            def write(self, raw):
                assert json.loads(path.read_text())['approvalBindings'][0]['resolved'] is True
                forwarded.set()
            def flush(self):pass
            def close(self):pass
        class Output:
            def __iter__(self):
                assert forwarded.wait(2)
                return iter([])
        child=type('Child',(),{'stdin':Input(),'stdout':Output(),'wait':lambda self:0})()
        response={'id':7,'result':{'decision':'decline'}}
        with patch.object(launcher.sys,'stdin',type('Stream',(),{'buffer':io.BytesIO((json.dumps(response)+'\n').encode())})()), patch.object(launcher.sys,'stdout',type('Stream',(),{'buffer':io.BytesIO()})()):
            launcher.proxy(child,self.path)
        self.assertEqual(self.get()['approvals'],[])
