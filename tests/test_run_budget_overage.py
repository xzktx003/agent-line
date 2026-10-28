import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from agent_line.store import TaskStore


ROOT = Path(__file__).resolve().parents[1]


class RunBudgetOverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore.initialize(Path(self.tmp.name) / 'tasks.sqlite')

    def payload(self, key, *, run, node, role, budget):
        return {
            'projectId': 'project', 'roleAgentId': role, 'roleBindingId': role,
            'workspace': f'/work/{key}',
            'workflow': {
                'id': 'workflow', 'revision': 'r1', 'runId': run, 'nodeId': node,
                'nodeExecutionId': key, 'attemptId': key,
            },
            'input': {'task': key},
            'limits': {'maxExecutionSeconds': 60, 'maxBusinessAttempts': 3},
            'runBudget': budget,
        }

    def register(self, key, **kwargs):
        return self.store.register(key, self.payload(key, **kwargs))[0]

    def summary(self, run):
        return self.store.run_budget_summary('project', 'workflow', run)

    def test_unused_and_exact_remaining_stay_unclamped_arithmetic(self):
        budget = {'maxSubmissions': 3, 'maxExecutionSeconds': 3600}
        first = self.register('within', run='within-run', node='node-a', role='role-a', budget=budget)
        self.assertTrue(self.store.claim(first['id'], first['payload']['workspace']))
        within = self.summary('within-run')
        self.assertEqual(within['state'], 'configured')
        self.assertEqual(within['submissionsUsed'], 1)
        self.assertEqual(within['remainingSubmissions'], 2)
        self.assertTrue(within['budgetAllowsSubmission'])
        self.assertEqual(within['usage'], {'tokens': None, 'modelCalls': None, 'cost': None})

        second = self.register('exact-b', run='within-run', node='node-b', role='role-b', budget=budget)
        self.assertTrue(self.store.claim(second['id'], second['payload']['workspace']))
        third = self.register('exact-c', run='within-run', node='node-c', role='role-c', budget=budget)
        self.assertTrue(self.store.claim(third['id'], third['payload']['workspace']))
        exact = self.summary('within-run')
        self.assertEqual(exact['submissionsUsed'], 3)
        self.assertEqual(exact['remainingSubmissions'], 0)
        self.assertFalse(exact['budgetAllowsSubmission'])

    def test_metering_delay_shows_submission_overage_without_stopping_work(self):
        budget = {'maxSubmissions': 1, 'maxExecutionSeconds': 3600}
        first = self.register('over-a', run='over-run', node='node-a', role='role-a', budget=budget)
        self.assertTrue(self.store.claim(first['id'], first['payload']['workspace']))
        blocked = self.register('over-b', run='over-run', node='node-b', role='role-b', budget=budget)
        self.assertFalse(self.store.claim(blocked['id'], blocked['payload']['workspace']))
        self.assertEqual(self.store.execution_queue(blocked['id'])['reason'], 'run_submission_limit')
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET execution_status='submitting' WHERE id=?", (blocked['id'],))
            db.execute(
                "INSERT INTO events(task_id,kind,created_at) VALUES (?, 'submission_intent', ?)",
                (blocked['id'], time.time()),
            )
        over = self.summary('over-run')
        self.assertEqual(over['state'], 'configured')
        self.assertEqual(over['submissionsUsed'], 2)
        self.assertEqual(over['maxSubmissions'], 1)
        self.assertEqual(over['remainingSubmissions'], -1)
        self.assertFalse(over['budgetAllowsSubmission'])
        self.assertEqual(over['usage'], {'tokens': None, 'modelCalls': None, 'cost': None})
        later = self.register('over-c', run='over-run', node='node-c', role='role-c', budget=budget)
        self.assertFalse(self.store.claim(later['id'], later['payload']['workspace']))
        self.assertEqual(self.store.execution_queue(later['id'])['reason'], 'run_submission_limit')

    def test_run_page_shows_overage_and_keeps_unused_remaining(self):
        done = subprocess.run(
            ['node', str(ROOT / 'tests' / 'run_budget_overage_check.mjs')],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == '__main__':
    unittest.main()
