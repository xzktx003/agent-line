import unittest

from agent_line.run_view import visible_submission_block


class VisibleSubmissionBlockTest(unittest.TestCase):
    def test_known_blocks_pass_through(self):
        for kind in (
            'role_revision_required', 'role_revision_mismatch',
            'role_revision_unavailable', 'execution_readiness_stale',
            'submission_rejected',
        ):
            self.assertEqual(
                visible_submission_block({'submissionBlock': kind}),
                {'submissionBlock': kind},
            )

    def test_missing_or_unknown_blocks_are_omitted(self):
        self.assertEqual(visible_submission_block({'executionStatus': 'registered'}), {})
        self.assertEqual(visible_submission_block({'submissionBlock': 'approval'}), {})
        self.assertEqual(visible_submission_block({'submissionBlock': ''}), {})
        self.assertEqual(visible_submission_block(None), {})
        self.assertEqual(visible_submission_block(['role_revision_required']), {})


if __name__ == '__main__':
    unittest.main()
