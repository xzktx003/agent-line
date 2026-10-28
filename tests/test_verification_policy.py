import copy
import unittest

from agent_line.verification_policy import policy_hash, validate_policy


def policy():
    return {'schemaVersion': 1, 'policyId': 'artifact-v1', 'kind': 'artifact_sha256',
            'requireTests': False, 'artifacts': [{'path': 'result.txt', 'sha256': 'a'*64}]}


class VerificationPolicyTests(unittest.TestCase):
    def test_normalization_is_stable_without_mutating_source(self):
        original = policy(); normalized = validate_policy(original)
        self.assertNotIn('maxBytes', original['artifacts'][0])
        self.assertEqual(normalized['artifacts'][0]['maxBytes'], 10_000_000)
        self.assertIsNone(normalized['artifacts'][0]['exactSize'])
        self.assertEqual(policy_hash(original), policy_hash(normalized))
        changed = copy.deepcopy(original); changed['artifacts'][0]['sha256'] = 'b'*64
        self.assertNotEqual(policy_hash(original), policy_hash(changed))

    def test_test_claims_unknown_fields_and_unbounded_policies_are_rejected(self):
        cases = [policy() | {'requireTests': True}, policy() | {'requireTests': 0},
                 policy() | {'schemaVersion': True}, policy() | {'command': 'pytest'},
                 policy() | {'kind': 'model_report'}, policy() | {'artifacts': []},
                 policy() | {'artifacts': policy()['artifacts']*2}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError): validate_policy(value)
        for item in ({'path': '../escape'}, {'maxBytes': True}, {'exactSize': False},
                     {'maxBytes': 64_000_001}, {'sha256': 'not-a-hash'}, {'command': 'pytest'}):
            value = policy(); value['artifacts'][0].update(item)
            with self.subTest(item=item), self.assertRaises(ValueError): validate_policy(value)
