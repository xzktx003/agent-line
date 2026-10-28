"""Plugin page destinations cannot redirect to arbitrary or credential-bearing URLs."""
from pathlib import Path
import subprocess
import unittest


class PluginNavigationTests(unittest.TestCase):
    def test_only_existing_same_origin_pages_are_accepted(self):
        source = Path(__file__).resolve().parents[1] / 'plugins/agent-line/dist/index.js'
        script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const sandbox = {window:{QwenPaw:{host:{React:{createElement(){}}},route:{add(){}},menu:{add(){}}}}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8') + '\nthis.paths = navigationPaths;', sandbox);
assert.deepEqual(JSON.parse(JSON.stringify(sandbox.paths({workbenchPath:'/workbench/',simEditorPath:'/workspace'}))),
                 {workbench:'/workbench/',sim:'/workspace'});
for (const value of [null, {}, {simEditorPath:'/workspace'},
  ...['https://other.test/workbench/', '//other.test/workbench/', '/workbench/?token=secret',
      '/workbench/#secret', '/workbench/../qwenpaw/', '/workspace', '/workbench',
      'javascript:alert(1)', '\\other.test\\workbench\\'].map(workbenchPath=>({workbenchPath,simEditorPath:'/workspace'})),
  ...['https://other.test/workspace', '/workspace-fake', '/workspace?token=secret', '/workbench/']
     .map(simEditorPath=>({workbenchPath:'/workbench/',simEditorPath}))]) {
  assert.throws(()=>sandbox.paths(value));
}
'''
        result = subprocess.run(['node', '-e', script, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
