import copy
import unittest

from agent_line.output_contract import blocks_success, flow_output, inspect_output


def task(schema, text, fields=None):
    payload = {'outputSchemaId': schema}
    if fields is not None:
        payload['outputFields'] = fields
    return {
        'executionStatus': 'succeeded',
        'payload': payload,
        'output': {'text': text, 'nativeStatus': 'task_complete', 'toolEvidence': []},
    }


CODING = '{"summary":"已完成","status":"completed","changedFiles":[]}'
FIELDS = {'fields': [{'name': 'summary', 'type': 'string', 'required': True}]}


class OutputContractTests(unittest.TestCase):
    def test_raw_json_keeps_declared_match_and_original_text(self):
        item = task('coding-result-v1', CODING)
        before = copy.deepcopy(item['output'])
        validation, structured = inspect_output(item)
        self.assertEqual(validation['reason'], 'declared_business_shape_matched')
        self.assertEqual(structured['status'], 'completed')
        self.assertEqual(item['output'], before)
        projected = flow_output(item)
        self.assertEqual(projected['text'], CODING)
        self.assertEqual(projected['result'], structured)
        self.assertFalse(blocks_success(item))

    def test_single_json_fence_projects_same_body_without_rewriting_text(self):
        for wrapped in (
            '```json\n' + CODING + '\n```',
            '```json\n' + CODING + '\n```\n',
            '```\n' + CODING + '\n```',
            '```json\r\n' + CODING + '\r\n```\r\n',
            '```json\n{\n  "summary": "已完成",\n  "status": "completed",\n  "changedFiles": []\n}\n```\n',
        ):
            with self.subTest(wrapped=wrapped):
                item = task('coding-result-v1', wrapped)
                before = copy.deepcopy(item)
                validation, structured = inspect_output(item)
                self.assertEqual(validation['state'], 'valid')
                self.assertEqual(validation['reason'], 'same_body_fence_unwrapped')
                self.assertEqual(structured, {'summary': '已完成', 'status': 'completed', 'changedFiles': []})
                self.assertEqual(item, before)
                projected = flow_output(item)
                self.assertEqual(projected['text'], wrapped)
                self.assertEqual(projected['result'], structured)
                self.assertFalse(blocks_success(item))

    def test_review_and_fields_use_the_same_fence_projection(self):
        review = '```json\n{"summary":"需要处理","approved":false,"issues":["边界"]}\n```\n'
        item = task('review-result-v1', review)
        validation, structured = inspect_output(item)
        self.assertEqual(validation['reason'], 'same_body_fence_unwrapped')
        self.assertEqual(structured['approved'], False)
        fields = task('fields-result-v1', '```\n{"summary":"ok"}\n```', FIELDS)
        validation, structured = inspect_output(fields)
        self.assertEqual(validation['reason'], 'same_body_fence_unwrapped')
        self.assertEqual(structured, {'summary': 'ok'})

    def test_fence_does_not_guess_missing_or_extra_content(self):
        rejected = [
            '说明\n```json\n' + CODING + '\n```\n',
            '```json\n' + CODING + '\n```\n补充',
            '```json\n' + CODING + '\n```\n\n',
            '```json\n' + CODING + '\n```\n```\n' + CODING + '\n```',
            '```JSON\n' + CODING + '\n```\n',
            '```json ' + CODING + '```',
            '```json\n' + CODING[:-1] + ',}\n```\n',
            '```json\n{"summary":"已完成","status":"completed"}\n```\n',
            '```json\n' + CODING[:-1] + ',"extra":true}\n```\n',
            '```json\n{"summary":"已完成","summary":"重复","status":"completed","changedFiles":[]}\n```\n',
        ]
        for text in rejected:
            with self.subTest(text=text):
                item = task('coding-result-v1', text)
                validation, structured = inspect_output(item)
                self.assertEqual(validation, {
                    'schemaId': 'coding-result-v1',
                    'state': 'invalid',
                    'reason': 'business_result_invalid',
                })
                self.assertIsNone(structured)
                self.assertTrue(blocks_success(item))
                with self.assertRaises(ValueError):
                    flow_output(item)

    def test_json_containing_fence_markers_is_not_unwrapped_when_already_valid(self):
        text = '{"summary":"包含 ``` 标记","status":"completed","changedFiles":[]}'
        validation, structured = inspect_output(task('coding-result-v1', text))
        self.assertEqual(validation['reason'], 'declared_business_shape_matched')
        self.assertIn('```', structured['summary'])


if __name__ == '__main__':
    unittest.main()
