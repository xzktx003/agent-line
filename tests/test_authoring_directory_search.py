"""Focused search and live metadata coverage for the authoring directory."""
import unittest


class AuthoringDirectorySearchTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing real-journal / fake-Sim HTTP fixture without
        # duplicating its ownership, project-scope, or credential setup.
        from tests.test_authoring_api import AuthoringAPITests
        self.fixture = AuthoringAPITests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def create(self, operation_id, name):
        response = self.fixture.create(op=operation_id, name=name)
        self.assertEqual(response.status_code, 202)
        return response.json()['receipt']['workflowId']

    def list_workflows(self, **params):
        return self.fixture.http.get(
            '/extension/v1/authoring/projects/one/workflows',
            headers={'Authorization': 'Bearer edit'}, params=params)

    def test_search_trims_and_casefolds_name_and_description(self):
        name_match = self.create('name-match', '中文Ops流程')
        description_match = self.create('description-match', '其他流程')
        self.fixture.remote.details[name_match]['description'] = 'Pipeline for QA'
        self.fixture.remote.details[description_match]['description'] = '需要处理大小写混合'

        chinese = self.list_workflows(search='  中文  ')
        self.assertEqual(chinese.status_code, 200)
        self.assertEqual([item['id'] for item in chinese.json()['workflows']], [name_match])
        self.assertEqual(chinese.json()['search'], '中文')
        english = self.list_workflows(search='  pIpElInE ')
        self.assertEqual([item['id'] for item in english.json()['workflows']], [name_match])
        folded = self.list_workflows(search='大小写混合')
        self.assertEqual([item['id'] for item in folded.json()['workflows']], [description_match])

    def test_empty_filtered_page_still_advances_scan_cursor(self):
        self.create('cursor-one', '匹配流程甲')
        self.create('cursor-two', '匹配流程乙')

        first = self.list_workflows(search='不存在', limit=1).json()
        self.assertEqual(first['workflows'], [])
        self.assertEqual(first['scannedCount'], 1)
        self.assertEqual(first['search'], '不存在')
        self.assertIsNotNone(first['nextCursor'])

        second = self.list_workflows(search='不存在', limit=1,
                                     after=first['nextCursor']).json()
        self.assertEqual(second['workflows'], [])
        self.assertEqual(second['scannedCount'], 1)
        self.assertNotEqual(second['nextCursor'], first['nextCursor'])

    def test_project_scope_hides_other_projects_even_with_shared_workspace(self):
        workflow = self.create('only-project-one', '仅项目一')
        other_project = self.fixture.request(
            'GET', '/projects/two/workflows', token='other')
        self.assertEqual(other_project.status_code, 200)
        self.assertEqual(other_project.json()['workflows'], [])
        denied_scope = self.fixture.request(
            'GET', '/projects/one/workflows', token='other')
        self.assertEqual(denied_scope.status_code, 404)
        self.assertNotIn(workflow, [item['id'] for item in other_project.json()['workflows']])

    def test_reads_return_only_valid_live_metadata_and_do_not_write_journal(self):
        valid = self.create('metadata-valid', '有效元数据')
        invalid = self.create('metadata-invalid', '无效元数据')
        self.fixture.remote.details[valid].update(
            lastRunAt='2026-10-22T08:30:00Z', runCount=4)
        self.fixture.remote.details[invalid].update(
            lastRunAt=1730000000, runCount=True)
        with self.fixture.journal.connect() as db:
            before = db.execute('SELECT count(*) FROM operations').fetchone()[0]

        response = self.list_workflows()
        self.assertEqual(response.status_code, 200)
        items = {item['id']: item for item in response.json()['workflows']}
        self.assertEqual(items[valid]['lastRunAt'], '2026-10-22T08:30:00Z')
        self.assertEqual(items[valid]['runCount'], 4)
        self.assertIsNone(items[invalid]['lastRunAt'])
        self.assertIsNone(items[invalid]['runCount'])
        with self.fixture.journal.connect() as db:
            after = db.execute('SELECT count(*) FROM operations').fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(self.fixture.remote.count('create'), 2)
        self.assertEqual(self.fixture.remote.count('detail'), 2)

    def test_invalid_search_and_cursor_parameters_are_rejected(self):
        too_long = self.list_workflows(search='x' * 256)
        self.assertEqual(too_long.status_code, 422)
        invalid_cursor = self.list_workflows(after='contains space')
        self.assertEqual(invalid_cursor.status_code, 422)


if __name__ == '__main__':
    unittest.main()
