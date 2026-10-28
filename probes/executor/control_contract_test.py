"""Unit checks for identity ambiguity and concurrent-work refusal; no Docker calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'deploy/executor'))
import control

class ControlContract(unittest.TestCase):
    def test_refuses_unbound_ambiguous_pending_and_other_turn(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(control,'STATE',Path(directory)), patch.object(control,'cleanup') as cleanup:
            workspace=str(Path(directory)/'work')
            with self.assertRaises(RuntimeError):control.proof('thread','turn',workspace,stop=True)
            record={'owner':'a','uid':os.getuid(),'workspace':workspace,'pendingTurns':0,'accepting':True,'identities':[{'codexThreadId':'thread','codexTurnId':'turn','nativeTerminal':False}]}
            def save(name,data):
                path=Path(directory)/(name+'.json');path.write_text(json.dumps(data));path.chmod(0o600)
            save('a',record);save('b',{**record,'owner':'b'})
            with self.assertRaises(RuntimeError):control.proof('thread','turn',workspace,stop=True)
            (Path(directory)/'b.json').unlink()
            save('a',{**record,'pendingTurns':1})
            with self.assertRaises(RuntimeError):control.proof('thread','turn',workspace,stop=True)
            save('a',{**record,'identities':record['identities']+[{'codexThreadId':'other','codexTurnId':'other','nativeTerminal':False}]})
            with self.assertRaises(RuntimeError):control.proof('thread','turn',workspace,stop=True)
            cleanup.assert_not_called()
            save('a',record)
            result=control.proof('thread','turn',workspace,stop=True)
            cleanup.assert_called_once()
            self.assertFalse(json.loads((Path(directory)/'a.json').read_text())['accepting'])
            self.assertFalse(result['resourcesReleased'])

if __name__=='__main__':unittest.main()
