import tempfile
import unittest
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
from agent_line.workbench import mount_workbench


class WorkbenchTests(unittest.TestCase):
    def test_static_workbench_does_not_expose_parent_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = root / "frontend"
            assets.mkdir()
            (assets / "index.html").write_text('<html lang="zh-CN">工作台</html>')
            (root / "secret.json").write_text("private-fixture")
            app = FastAPI()
            mount_workbench(app, assets)
            with TestClient(app) as client:
                response = client.get("/workbench/")
                self.assertEqual(response.status_code, 200)
                self.assertIn("工作台", response.text)
                self.assertIn(
                    "connect-src 'self'", response.headers["content-security-policy"]
                )
                response = client.get("/workbench/%2e%2e/secret.json")
                self.assertNotIn("private-fixture", response.text)
                self.assertEqual(response.status_code, 404)

    def test_missing_frontend_is_explicit_startup_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                mount_workbench(FastAPI(), Path(directory))
