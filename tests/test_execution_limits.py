import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from agent_line.store import Conflict, TaskStore


class ExecutionLimitsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'tasks.sqlite'
        self.store = TaskStore.initialize(self.path)

    def payload(self, key, *, run='run', node='node', revision='r1', role='role',
                limits=None, scope=True, workspace=None, run_budget=None):
        workflow = {'runId': run, 'nodeExecutionId': key, 'attemptId': key}
        if scope:
            workflow.update({'id': 'workflow', 'revision': revision, 'nodeId': node})
        result = {'projectId': 'project', 'roleAgentId': role, 'roleBindingId': role,
                  'workspace': workspace or f'/work/{key}', 'workflow': workflow, 'input': {'task': key}}
        if limits is not None:
            result['limits'] = limits
        if run_budget is not None:
            result['runBudget'] = run_budget
        return result

    def register(self, key, **kwargs):
        return self.store.register(key, self.payload(key, **kwargs))[0]

    def complete_claim(self, task):
        self.assertTrue(self.store.claim(task['id'], task['payload']['workspace']))
        self.store.accept(task['id'], {'upstreamTaskId': 'upstream-' + task['id']})
        self.store.finish(task['id'], 'failed', None, [
            {'kind': 'execution_terminal', 'executionId': 'upstream-' + task['id'], 'confirmed': True},
        ])

    def test_legacy_records_keep_existing_behavior_and_have_no_projection(self):
        task = self.register('legacy')
        self.assertIsNone(self.store.execution_limits(task['id']))
        self.assertFalse(self.store.execution_deadline_exceeded(task['id']))
        self.assertTrue(self.store.claim(task['id'], task['payload']['workspace']))

    def test_limited_registration_validates_strict_bounds_and_complete_scope(self):
        good = {'maxExecutionSeconds': 20, 'maxBusinessAttempts': 2}
        for bad in (
            {'maxExecutionSeconds': True, 'maxBusinessAttempts': 2},
            {'maxExecutionSeconds': 0, 'maxBusinessAttempts': 2},
            {'maxExecutionSeconds': 20, 'maxBusinessAttempts': 101},
            {'maxExecutionSeconds': 20},
            {'maxExecutionSeconds': 20, 'maxBusinessAttempts': 2, 'extra': 1},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.register('bad-' + str(len(str(bad))), limits=bad)
        with self.assertRaises(ValueError):
            self.register('missing-scope', limits=good, scope=False)
        with self.assertRaises(ValueError):
            self.register('missing-revision', limits=good, revision='')

    def test_first_limit_freezes_across_revision_and_role(self):
        self.register('first', limits={'maxExecutionSeconds': 20, 'maxBusinessAttempts': 2})
        self.register('second', revision='r2', role='changed-role',
                      limits={'maxExecutionSeconds': 20, 'maxBusinessAttempts': 2})
        with self.assertRaises(Conflict):
            self.register('widen', revision='r3', role='other',
                          limits={'maxExecutionSeconds': 20, 'maxBusinessAttempts': 3})
        with self.assertRaises(Conflict):
            self.register('narrow', limits={'maxExecutionSeconds': 20, 'maxBusinessAttempts': 1})
        with self.assertRaises(Conflict):
            self.register('longer', limits={'maxExecutionSeconds': 21, 'maxBusinessAttempts': 2})

    def test_legacy_group_stays_readable_but_cannot_gain_a_new_frozen_quota(self):
        legacy = self.register('legacy')
        with self.assertRaises(Conflict):
            self.register('limited', limits={'maxExecutionSeconds': 20, 'maxBusinessAttempts': 2})
        self.assertIsNone(self.store.execution_limits(legacy['id']))

    def test_legacy_attempt_cannot_join_a_limited_group_by_omitting_limits(self):
        self.register('first', limits={'maxExecutionSeconds': 20, 'maxBusinessAttempts': 2})
        with self.assertRaises(Conflict):
            self.register('legacy-retry')

    def test_intent_count_is_permanent_and_per_node(self):
        limits = {'maxExecutionSeconds': 30, 'maxBusinessAttempts': 1}
        first = self.register('first', limits=limits)
        other = self.register('other-node', node='other-node', role='independent-role', limits=limits)
        self.complete_claim(first)
        retry = self.register('retry', limits=limits, revision='r2', role='new-role')
        self.assertFalse(self.store.claim(retry['id'], retry['payload']['workspace']))
        self.assertEqual(self.store.execution_queue(retry['id'])['reason'], 'business_attempt_limit')
        self.assertTrue(self.store.claim(other['id'], other['payload']['workspace']))
        projection = self.store.execution_limits(retry['id'])
        self.assertEqual(projection['attemptsUsed'], 1)
        self.assertEqual(projection['maxBusinessAttempts'], 1)
        self.assertFalse(projection['admissionAllowed'])
        self.assertEqual(projection['usage'], {'tokens': None, 'modelCalls': None, 'cost': None})

    def test_concurrent_claims_reserve_only_frozen_attempt_count(self):
        limits = {'maxExecutionSeconds': 30, 'maxBusinessAttempts': 2}
        tasks = [self.register(str(i), limits=limits, node='node', role='role-' + str(i))
                 for i in range(6)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            claims = list(pool.map(lambda task: TaskStore(self.path).claim(
                task['id'], task['payload']['workspace']), tasks))
        self.assertEqual(sum(claims), 2)

    def test_concurrent_claims_reserve_only_frozen_run_submissions(self):
        limits = {'maxExecutionSeconds': 30, 'maxBusinessAttempts': 6}
        budget = {'maxSubmissions': 2, 'maxExecutionSeconds': 3600}
        tasks = [self.register(str(i), limits=limits, run_budget=budget, run='shared-run',
                               node='node-' + str(i), role='role-' + str(i))
                 for i in range(6)]

        gate = threading.Barrier(len(tasks))

        def attempt(task):
            local = TaskStore(self.path)
            before = local.run_budget(task['id'])
            gate.wait(5)
            claimed = local.claim(task['id'], task['payload']['workspace'])
            return before['submissionsUsed'], before['admissionAllowed'], claimed

        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            attempts = list(pool.map(attempt, tasks))
        claimed = [item[2] for item in attempts]
        self.assertEqual(sum(claimed), 2)
        self.assertTrue(all(used == 0 and allowed for used, allowed, _claimed in attempts))
        winners = [task for task, ok in zip(tasks, claimed) if ok]
        losers = [task for task, ok in zip(tasks, claimed) if not ok]
        self.assertEqual(len(winners), 2)
        for task in winners:
            self.assertEqual(self.store.get(task['id'])['executionStatus'], 'submitting')
        for task in losers:
            self.assertEqual(self.store.get(task['id'])['executionStatus'], 'registered')
            self.assertEqual(self.store.execution_queue(task['id'])['reason'], 'run_submission_limit')
        with self.store.connect() as db:
            intents = db.execute("SELECT COUNT(*) FROM events WHERE kind='submission_intent'").fetchone()[0]
        self.assertEqual(intents, 2)
        tail = self.register('tail', limits=limits, run_budget=budget, run='shared-run',
                             node='tail-node', role='tail-role')
        self.assertEqual(tail['executionStatus'], 'registered')
        self.assertFalse(self.store.claim(tail['id'], tail['payload']['workspace']))
        self.assertEqual(self.store.get(tail['id'])['executionStatus'], 'registered')
        snapshot = self.store.run_budget(winners[0]['id'])
        self.assertEqual(snapshot['submissionsUsed'], 2)
        self.assertEqual(snapshot['usage'], {'tokens': None, 'modelCalls': None, 'cost': None})

    def test_unstarted_cancel_does_not_consume_an_attempt(self):
        limits = {'maxExecutionSeconds': 30, 'maxBusinessAttempts': 1}
        canceled = self.register('canceled', limits=limits)
        self.assertTrue(self.store.request_cancel(canceled['id']))
        next_task = self.register('next', limits=limits)
        self.assertTrue(self.store.claim(next_task['id'], next_task['payload']['workspace']))

    def test_uncertain_and_cancel_after_intent_do_not_refund(self):
        limits = {'maxExecutionSeconds': 30, 'maxBusinessAttempts': 1}
        uncertain = self.register('uncertain', limits=limits, node='uncertain-node')
        self.assertTrue(self.store.claim(uncertain['id'], uncertain['payload']['workspace']))
        self.store.mark_submission_uncertain(uncertain['id'])
        uncertain_retry = self.register('uncertain-retry', limits=limits, node='uncertain-node')
        self.assertFalse(self.store.claim(uncertain_retry['id'], uncertain_retry['payload']['workspace']))

        canceled = self.register('cancel-requested', limits=limits, node='canceled-node',
                                 role='cancel-role')
        self.assertTrue(self.store.claim(canceled['id'], canceled['payload']['workspace']))
        self.store.accept(canceled['id'], {'upstreamTaskId': 'upstream-canceled'})
        self.assertTrue(self.store.request_cancel(canceled['id']))
        canceled_retry = self.register('canceled-retry', limits=limits, node='canceled-node')
        self.assertFalse(self.store.claim(canceled_retry['id'], canceled_retry['payload']['workspace']))

    def test_submitted_cancel_with_deleted_intent_is_unknown_not_a_refund(self):
        limits = {'maxExecutionSeconds': 30, 'maxBusinessAttempts': 1}
        submitted = self.register('submitted', limits=limits)
        self.assertTrue(self.store.claim(submitted['id'], submitted['payload']['workspace']))
        self.store.accept(submitted['id'], {'upstreamTaskId': 'upstream-submitted'})
        self.store.request_cancel(submitted['id'])
        with self.store.connect() as db:
            db.execute("DELETE FROM events WHERE task_id=? AND kind='submission_intent'",
                       (submitted['id'],))
        retry = self.register('submitted-retry', limits=limits)
        self.assertEqual(self.store.execution_queue(retry['id'])['reason'], 'state_unknown')
        self.assertFalse(self.store.claim(retry['id'], retry['payload']['workspace']))
        with self.assertRaises(ValueError):
            self.store.execution_limits(retry['id'])
        with self.assertRaises(ValueError):
            self.store.execution_deadline_exceeded(submitted['id'])

    def test_registered_intent_and_reordered_intent_history_fail_closed(self):
        limits = {'maxExecutionSeconds': 30, 'maxBusinessAttempts': 2}
        registered = self.register('registered-intent', limits=limits, node='registered-node')
        with self.store.connect() as db:
            db.execute("INSERT INTO events(task_id,kind,created_at) VALUES (?, 'submission_intent', ?)",
                       (registered['id'], time.time()))
        blocked = self.register('registered-retry', limits=limits, node='registered-node')
        self.assertEqual(self.store.execution_queue(blocked['id'])['reason'], 'state_unknown')
        self.assertFalse(self.store.claim(blocked['id'], blocked['payload']['workspace']))

        reordered = self.register('reordered', limits=limits, node='reordered-node', role='reordered-role')
        self.assertTrue(self.store.claim(reordered['id'], reordered['payload']['workspace']))
        with self.store.connect() as db:
            db.execute("DELETE FROM events WHERE task_id=? AND kind='registered'", (reordered['id'],))
            db.execute("INSERT INTO events(task_id,kind,created_at) VALUES (?, 'registered', ?)",
                       (reordered['id'], time.time()))
        reordered_retry = self.register('reordered-retry', limits=limits, node='reordered-node',
                                        role='another-reordered-role')
        self.assertEqual(self.store.execution_queue(reordered_retry['id'])['reason'], 'state_unknown')
        self.assertFalse(self.store.claim(reordered_retry['id'], reordered_retry['payload']['workspace']))

    def test_execution_event_before_submission_intent_fails_closed(self):
        limits = {'maxExecutionSeconds': 30, 'maxBusinessAttempts': 2}
        task = self.register('accepted-first', limits=limits, node='accepted-node', role='accepted-role')
        self.assertTrue(self.store.claim(task['id'], task['payload']['workspace']))
        self.store.accept(task['id'], {'upstreamTaskId': 'upstream-accepted-first'})
        with self.store.connect() as db:
            db.execute("DELETE FROM events WHERE task_id=? AND kind IN ('submission_intent', 'accepted')",
                       (task['id'],))
            db.execute("INSERT INTO events(task_id,kind,created_at) VALUES (?, 'accepted', ?)",
                       (task['id'], time.time()))
            db.execute("INSERT INTO events(task_id,kind,created_at) VALUES (?, 'submission_intent', ?)",
                       (task['id'], time.time()))
        retry = self.register('accepted-first-retry', limits=limits, node='accepted-node',
                              role='another-accepted-role')
        self.assertEqual(self.store.execution_queue(retry['id'])['reason'], 'state_unknown')
        self.assertFalse(self.store.claim(retry['id'], retry['payload']['workspace']))
        with self.assertRaises(ValueError):
            self.store.execution_deadline_exceeded(task['id'])

    def test_exhausted_pending_head_does_not_block_another_node(self):
        limits = {'maxExecutionSeconds': 30, 'maxBusinessAttempts': 1}
        prior = self.register('prior', limits=limits, node='limited-node', role='prior-role',
                              workspace='/shared')
        exhausted = self.register('exhausted', limits=limits, node='limited-node', role='shared-role',
                                 workspace='/shared')
        self.assertTrue(self.store.claim(prior['id'], '/shared'))
        self.store.accept(prior['id'], {'upstreamTaskId': 'upstream-prior'})
        self.store.finish(prior['id'], 'failed', None, [
            {'kind': 'execution_terminal', 'executionId': 'upstream-prior', 'confirmed': True},
        ])
        with self.store.connect() as db:
            db.execute('UPDATE tasks SET resources_released=1 WHERE id=?', (prior['id'],))
            db.execute('DELETE FROM workspace_leases WHERE task_id=?', (prior['id'],))
        self.assertEqual(self.store.execution_queue(exhausted['id'])['reason'], 'business_attempt_limit')
        independent = self.register('independent-node', limits=limits, node='another-node',
                                    role='shared-role', workspace='/shared')
        self.assertTrue(self.store.claim(independent['id'], '/shared'))

    def test_deadline_starts_at_submission_intent_and_terminal_is_not_exceeded(self):
        task = self.register('deadline', limits={'maxExecutionSeconds': 1, 'maxBusinessAttempts': 2})
        self.assertIsNone(self.store.execution_limits(task['id'])['deadlineAt'])
        self.assertFalse(self.store.execution_deadline_exceeded(task['id']))
        self.assertTrue(self.store.claim(task['id'], task['payload']['workspace']))
        with self.store.connect() as db:
            db.execute("UPDATE events SET created_at=? WHERE task_id=? AND kind='submission_intent'",
                       (time.time() - 5, task['id']))
        projection = self.store.execution_limits(task['id'])
        self.assertLess(projection['deadlineAt'], time.time())
        self.assertTrue(self.store.execution_deadline_exceeded(task['id']))
        self.store.accept(task['id'], {'upstreamTaskId': 'upstream'})
        self.store.finish(task['id'], 'failed', None, [
            {'kind': 'execution_terminal', 'executionId': 'upstream', 'confirmed': True},
        ])
        self.assertFalse(self.store.execution_deadline_exceeded(task['id']))
        self.assertEqual(self.store.execution_limits(task['id'])['deadlineAt'], projection['deadlineAt'])

    def test_active_limited_task_requires_one_valid_nonfuture_intent(self):
        task = self.register('active', limits={'maxExecutionSeconds': 5, 'maxBusinessAttempts': 2})
        self.assertFalse(self.store.execution_deadline_exceeded(task['id']))
        self.assertTrue(self.store.claim(task['id'], task['payload']['workspace']))
        with self.store.connect() as db:
            db.execute("DELETE FROM events WHERE task_id=? AND kind='submission_intent'", (task['id'],))
        with self.assertRaises(ValueError):
            self.store.execution_deadline_exceeded(task['id'])
        with self.store.connect() as db:
            db.execute("INSERT INTO events(task_id,kind,created_at) VALUES (?, 'submission_intent', ?)",
                       (task['id'], time.time()))
        with self.store.connect() as db:
            db.execute("UPDATE events SET created_at=? WHERE task_id=? AND kind='submission_intent'",
                       (time.time() + 10, task['id']))
        with self.assertRaises(ValueError):
            self.store.execution_deadline_exceeded(task['id'])

        duplicate = self.register('duplicate', limits={'maxExecutionSeconds': 5, 'maxBusinessAttempts': 2},
                                  node='duplicate-node', role='duplicate-role')
        self.assertTrue(self.store.claim(duplicate['id'], duplicate['payload']['workspace']))
        with self.store.connect() as db:
            db.execute("INSERT INTO events(task_id,kind,created_at) SELECT task_id,kind,created_at FROM events WHERE task_id=? AND kind='submission_intent'",
                       (duplicate['id'],))
        with self.assertRaises(ValueError):
            self.store.execution_deadline_exceeded(duplicate['id'])
        blocked = self.register('after-duplicate', limits={'maxExecutionSeconds': 5, 'maxBusinessAttempts': 2},
                                node='duplicate-node', role='another-role')
        self.assertEqual(self.store.execution_queue(blocked['id'])['reason'], 'state_unknown')
        self.assertFalse(self.store.claim(blocked['id'], blocked['payload']['workspace']))
        with self.assertRaises(ValueError):
            self.store.execution_limits(blocked['id'])

        negative = self.register('negative', limits={'maxExecutionSeconds': 5, 'maxBusinessAttempts': 2},
                                 node='negative-node', role='negative-role')
        self.assertTrue(self.store.claim(negative['id'], negative['payload']['workspace']))
        with self.store.connect() as db:
            db.execute("UPDATE events SET created_at=-1 WHERE task_id=? AND kind='submission_intent'",
                       (negative['id'],))
        with self.assertRaises(ValueError):
            self.store.execution_deadline_exceeded(negative['id'])
        negative_retry = self.register('negative-retry', limits={'maxExecutionSeconds': 5, 'maxBusinessAttempts': 2},
                                       node='negative-node', role='negative-retry-role')
        self.assertEqual(self.store.execution_queue(negative_retry['id'])['reason'], 'state_unknown')
        self.assertFalse(self.store.claim(negative_retry['id'], negative_retry['payload']['workspace']))
        with self.assertRaises(ValueError):
            self.store.execution_limits(negative_retry['id'])



    def intent_times(self, task_id):
        return [event['created_at'] for event in self.store.events(task_id)
                if event['kind'] == 'submission_intent']

    def reject_before_acceptance(self, task):
        self.assertTrue(self.store.claim(task['id'], task['payload']['workspace']))
        started = self.intent_times(task['id'])
        self.assertEqual(len(started), 1)
        self.assertTrue(self.store.record_unaccepted_rejection(task['id']))
        self.assertEqual(self.store.get(task['id'])['executionStatus'], 'registered')
        return started[0]

    def test_unaccepted_rejection_counts_once_and_corrected_resubmit_reuses_it(self):
        limits = {'maxExecutionSeconds': 30, 'maxBusinessAttempts': 1}
        task = self.register('rejected', limits=limits)
        started = self.reject_before_acceptance(task)
        projection = self.store.execution_limits(task['id'])
        self.assertEqual(projection['attemptsUsed'], 1)
        self.assertEqual(projection['deadlineAt'], started + 30)
        self.assertEqual(projection['usage'], {'tokens': None, 'modelCalls': None, 'cost': None})
        retry = self.register('retry', limits=limits)
        self.assertEqual(self.store.execution_queue(retry['id'])['reason'], 'business_attempt_limit')
        self.assertFalse(self.store.claim(retry['id'], retry['payload']['workspace']))
        self.assertTrue(self.store.claim(task['id'], task['payload']['workspace'], consume_rejection=True))
        self.assertEqual(self.intent_times(task['id']), [started])
        reused = self.store.execution_limits(task['id'])
        self.assertEqual(reused['attemptsUsed'], 1)
        self.assertEqual(reused['deadlineAt'], started + 30)
        self.assertEqual(reused['usage'], {'tokens': None, 'modelCalls': None, 'cost': None})
        released = [event for event in self.store.events(task['id'])
                    if event['kind'] == 'submission_rejection_released']
        self.assertEqual(len(released), 1)

        bare = self.register('bare', node='bare-node', role='bare-role')
        self.assertTrue(self.store.claim(bare['id'], bare['payload']['workspace']))
        self.assertTrue(self.store.record_unaccepted_rejection(bare['id']))
        with self.store.connect() as db:
            db.execute("DELETE FROM events WHERE task_id=? AND kind='submission_intent'", (bare['id'],))
        self.assertFalse(self.store.claim(bare['id'], bare['payload']['workspace'], consume_rejection=True))
        self.assertEqual(self.store.get(bare['id'])['executionStatus'], 'registered')
        self.assertFalse(any(event['kind'] == 'submission_rejection_released'
                             for event in self.store.events(bare['id'])))
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM workspace_leases WHERE task_id=?',
                                         (bare['id'],)).fetchone())

    def test_unaccepted_rejection_shares_remaining_run_budget_with_nested_node(self):
        limits = {'maxExecutionSeconds': 60, 'maxBusinessAttempts': 3}
        budget = {'maxSubmissions': 2, 'maxExecutionSeconds': 3600}
        first = self.register('node-a', limits=limits, run_budget=budget, run='nested-run')
        started = self.reject_before_acceptance(first)
        snapshot = self.store.run_budget(first['id'])
        self.assertEqual(snapshot['submissionsUsed'], 1)
        self.assertEqual(snapshot['deadlineAt'], started + 3600)
        self.assertEqual(snapshot['usage'], {'tokens': None, 'modelCalls': None, 'cost': None})
        second = self.register('node-b', limits=limits, run_budget=budget, run='nested-run',
                               node='node-b', role='role-b')
        self.assertTrue(self.store.claim(second['id'], second['payload']['workspace']))
        self.assertEqual(self.store.run_budget(first['id'])['submissionsUsed'], 2)
        third = self.register('node-c', limits=limits, run_budget=budget, run='nested-run',
                              node='node-c', role='role-c')
        self.assertEqual(self.store.execution_queue(third['id'])['reason'], 'run_submission_limit')
        self.assertFalse(self.store.claim(third['id'], third['payload']['workspace']))

        limited = self.register('deadline-a', limits={'maxExecutionSeconds': 3600, 'maxBusinessAttempts': 3},
                                run_budget={'maxSubmissions': 5, 'maxExecutionSeconds': 30},
                                run='deadline-run', node='deadline-a', role='deadline-role-a')
        self.reject_before_acceptance(limited)
        with self.store.connect() as db:
            db.execute("UPDATE events SET created_at=? WHERE task_id=? AND kind='submission_intent'",
                       (time.time() - 100, limited['id']))
        nested = self.register('deadline-b', limits={'maxExecutionSeconds': 3600, 'maxBusinessAttempts': 3},
                               run_budget={'maxSubmissions': 5, 'maxExecutionSeconds': 30},
                               run='deadline-run', node='deadline-b', role='deadline-role-b')
        self.assertEqual(self.store.execution_queue(nested['id'])['reason'], 'run_execution_deadline')
        self.assertFalse(self.store.claim(nested['id'], nested['payload']['workspace']))
        self.assertFalse(self.store.claim(limited['id'], limited['payload']['workspace'],
                                          consume_rejection=True))

    def test_unaccepted_rejection_with_unknown_tail_fails_closed(self):
        limits = {'maxExecutionSeconds': 30, 'maxBusinessAttempts': 2}
        budget = {'maxSubmissions': 4, 'maxExecutionSeconds': 3600}
        task = self.register('tail', limits=limits, run_budget=budget, run='tail-run')
        self.reject_before_acceptance(task)
        with self.store.connect() as db:
            db.execute("UPDATE events SET payload=? WHERE task_id=? AND kind='submission_rejected'",
                       ('{"reason":"other"}', task['id']))
        self.assertEqual(self.store.execution_queue(task['id'])['reason'], 'submission_rejected')
        with self.assertRaises(ValueError):
            self.store.execution_limits(task['id'])
        with self.assertRaises(ValueError):
            self.store.run_budget(task['id'])
        with self.assertRaises(Conflict):
            self.register('tail-other', limits=limits, run_budget=budget, run='tail-run',
                          node='tail-other', role='tail-other-role')
        with self.store.connect() as db:
            db.execute("UPDATE events SET payload=? WHERE task_id=? AND kind='submission_rejected'",
                       ('{"reason":"submission_rejected_unaccepted"}', task['id']))
            db.execute("INSERT INTO events(task_id,kind,created_at) VALUES (?, 'accepted', ?)",
                       (task['id'], time.time()))
        self.assertEqual(self.store.execution_queue(task['id'])['reason'], 'state_unknown')
        with self.assertRaises(ValueError):
            self.store.execution_limits(task['id'])
        with self.assertRaises(ValueError):
            self.store.run_budget(task['id'])


if __name__ == '__main__':
    unittest.main()
