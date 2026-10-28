import hashlib
import tempfile
import unittest
from pathlib import Path

from agent_line.memory_applicability import (
    MemoryApplicability, build_applicability_router, drop_exact_hidden, filter_search_text,
)
from agent_line.store import Conflict


def digest(text):
    return hashlib.sha256(text.strip().encode()).hexdigest()


class MemoryApplicabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MemoryApplicability.initialize(Path(self.tmp.name) / "applicability.db")
        self.old = "old verified constraint"
        self.new = "corrected constraint"
        self.store.classify(
            role_id="role-1", project_id="project-a", experience_id="exp-old",
            content_sha256=digest(self.old), state="active", reason="reviewed",
        )

    def test_default_recall_keeps_only_active_and_unclassified(self):
        self.store.classify(
            role_id="role-1", project_id="project-a", experience_id="exp-new",
            content_sha256=digest("pending note"), state="candidate", reason="unreviewed",
        )
        hits = [
            {"experienceId": "exp-old", "text": self.old},
            {"experienceId": "exp-new", "text": "pending note"},
            {"experienceId": "exp-native", "text": "native unmarked"},
        ]
        kept = self.store.recall(role_id="role-1", project_id="project-a", hits=hits)
        self.assertEqual([hit["experienceId"] for hit in kept], ["exp-old", "exp-native"])
        self.assertEqual(kept[0]["label"], "正式")
        self.assertEqual(kept[1]["applicability"], "unclassified")

    def test_unverified_view_labels_candidates_and_still_drops_terminal(self):
        self.store.classify(
            role_id="role-1", project_id="project-a", experience_id="exp-new",
            content_sha256=digest("pending note"), state="candidate", reason="unreviewed",
        )
        self.store.classify(
            role_id="role-1", project_id="project-a", experience_id="exp-old",
            content_sha256=digest(self.old), state="rejected", reason="user rejected",
        )
        hits = [
            {"experienceId": "exp-new", "text": "pending note"},
            {"experienceId": "exp-old", "text": self.old},
        ]
        kept = self.store.recall(
            role_id="role-1", project_id="project-a", hits=hits, include_unverified=True,
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["label"], "待验证")
        self.assertEqual(kept[0]["applicability"], "candidate")

    def test_supersede_keeps_relationship_and_hides_old_fact(self):
        self.store.classify(
            role_id="role-1", project_id="project-a", experience_id="exp-new",
            content_sha256=digest(self.new), state="active", supersedes="exp-old", reason="user correction",
        )
        old = self.store.get("role-1", "project-a", "exp-old")
        new = self.store.get("role-1", "project-a", "exp-new")
        self.assertEqual(old["state"], "superseded")
        self.assertEqual(old["superseded_by"], "exp-new")
        self.assertEqual(new["supersedes"], "exp-old")
        kept = self.store.recall(
            role_id="role-1", project_id="project-a",
            hits=[{"experienceId": "exp-old", "text": self.old}, {"experienceId": "exp-new", "text": self.new}],
        )
        self.assertEqual([hit["experienceId"] for hit in kept], ["exp-new"])

    def test_other_project_does_not_receive_classified_memory(self):
        kept = self.store.recall(
            role_id="role-1", project_id="project-b",
            hits=[{"experienceId": "exp-old", "text": self.old}, {"experienceId": "exp-local", "text": "local unmarked"}],
        )
        self.assertEqual([hit["experienceId"] for hit in kept], ["exp-local"])
        self.assertEqual(kept[0]["applicability"], "unclassified")

    def test_terminal_experience_cannot_become_active_again(self):
        self.store.classify(
            role_id="role-1", project_id="project-a", experience_id="exp-old",
            content_sha256=digest(self.old), state="deleted", reason="user deleted",
        )
        with self.assertRaises(Conflict):
            self.store.classify(
                role_id="role-1", project_id="project-a", experience_id="exp-old",
                content_sha256=digest(self.old), state="active", reason="revive",
            )

    def test_exact_body_of_non_active_state_is_hidden(self):
        self.store.classify(
            role_id="role-1", project_id="project-a", experience_id="exp-old",
            content_sha256=digest(self.old), state="expired", reason="code changed",
        )
        self.assertTrue(self.store.exact_hidden("role-1", "project-a", self.old))
        self.assertFalse(self.store.exact_hidden("role-1", "project-a", self.old + " extra"))
        self.assertFalse(self.store.exact_hidden("role-1", "project-b", self.old))


    def test_drop_exact_hidden_keeps_fragments_and_active_bodies(self):
        self.store.classify(
            role_id="role-1", project_id="project-a", experience_id="exp-old",
            content_sha256=digest(self.old), state="deleted", reason="user deleted",
        )
        messages = [self.old, "prefix " + self.old, "active body"]
        self.store.classify(
            role_id="role-1", project_id="project-a", experience_id="exp-live",
            content_sha256=digest("active body"), state="active", reason="reviewed",
        )
        kept = drop_exact_hidden(self.store, "role-1", "project-a", messages, lambda item: item)
        self.assertEqual(kept, ["prefix " + self.old, "active body"])


    def test_unknown_id_does_not_carry_another_projects_body(self):
        kept = self.store.recall(
            role_id="role-1", project_id="project-b",
            hits=[{"experienceId": "exp-local", "text": self.old}],
        )
        self.assertEqual(kept, [])

    def test_search_text_drops_hidden_sections_and_keeps_fragments(self):
        self.store.classify(
            role_id="role-1", project_id="project-a", experience_id="exp-old",
            content_sha256=digest(self.old), state="deleted", reason="user deleted",
        )
        answer = "\n".join([
            "========== memory/a.md:1-1 [score=1] ==========",
            self.old,
            "========== memory/b.md:1-1 [score=1] ==========",
            "prefix " + self.old,
            "========== memory/c.md:1-1 [score=1] ==========",
            "native unmarked",
        ])
        filtered = filter_search_text(self.store, "role-1", "project-a", answer)
        self.assertNotIn("\n" + self.old + "\n", "\n" + filtered + "\n")
        self.assertIn("prefix " + self.old, filtered)
        self.assertIn("native unmarked", filtered)
        foreign = filter_search_text(self.store, "role-1", "project-b", self.old)
        self.assertEqual(foreign, "(no memory results)")
        self.assertEqual(filter_search_text(self.store, "role-1", "project-b", "local unmarked"), "local unmarked")

    def test_search_text_ignores_renderer_blank_lines_but_not_fragments(self):
        stored = "heading line\nbody line"
        rendered = "heading line\n\nbody line"
        self.store.classify(
            role_id="role-1", project_id="project-a", experience_id="exp-rendered",
            content_sha256=digest(stored), state="deleted", reason="replaced",
        )
        live = "current line\nstill current"
        self.store.classify(
            role_id="role-1", project_id="project-a", experience_id="exp-live",
            content_sha256=digest(live), state="active", reason="reviewed",
        )
        answer = "\n".join([
            "========== memory/old.md:1-2 [score=1] ==========",
            rendered,
            "========== memory/live.md:1-2 [score=1] ==========",
            live.replace("\n", "\n\n", 1),
            "========== memory/fragment.md:1-1 [score=1] ==========",
            "prefix " + rendered,
        ])
        filtered = filter_search_text(self.store, "role-1", "project-a", answer)
        self.assertNotIn("memory/old.md", filtered)
        self.assertNotIn("\n" + rendered + "\n", "\n" + filtered + "\n")
        self.assertIn(live.replace("\n", "\n\n", 1), filtered)
        self.assertIn("prefix " + rendered, filtered)
        self.assertFalse(self.store.exact_hidden("role-1", "project-a", rendered))



    def test_volatile_fact_cannot_stay_active_and_candidate_stays_hidden(self):
        with self.assertRaises(ValueError):
            self.store.classify(
                role_id="role-1", project_id="project-a", experience_id="exp-token",
                content_sha256=digest("token still valid"), state="active", reason="observed",
                fact_class="token_validity",
            )
        self.store.classify(
            role_id="role-1", project_id="project-a", experience_id="exp-token",
            content_sha256=digest("token still valid"), state="candidate", reason="observed",
            fact_class="token_validity",
        )
        kept = self.store.recall(
            role_id="role-1", project_id="project-a",
            hits=[{"experienceId": "exp-token", "text": "token still valid"},
                  {"experienceId": "exp-old", "text": self.old}],
        )
        self.assertEqual([hit["experienceId"] for hit in kept], ["exp-old"])

    def test_user_can_reject_a_candidate_and_still_read_the_record(self):
        self.store.classify(
            role_id="role-1", project_id="project-a", experience_id="exp-new",
            content_sha256=digest("pending note"), state="candidate", reason="unreviewed",
        )
        rejected = self.store.classify(
            role_id="role-1", project_id="project-a", experience_id="exp-new",
            content_sha256=digest("pending note"), state="rejected", reason="user rejected",
        )
        self.assertEqual(rejected["state"], "rejected")
        self.assertEqual(self.store.get("role-1", "project-a", "exp-new")["state"], "rejected")
        listed = self.store.list_for("role-1", "project-a")
        self.assertIn("exp-new", [row["experience_id"] for row in listed])

    def test_changed_conditions_expire_only_dependent_active_memory(self):
        self.store.classify(
            role_id="role-1", project_id="project-a", experience_id="exp-bound",
            content_sha256=digest("bound constraint"), state="active", reason="reviewed",
            conditions="branch main; deps qwenpaw@502b59d; structure plugins/agent-line",
        )
        self.store.classify(
            role_id="role-1", project_id="project-a", experience_id="exp-old",
            content_sha256=digest(self.old), state="active", reason="reviewed",
            conditions="branch main; deps qwenpaw@502b59d; structure plugins/agent-line",
        )
        unchanged = self.store.reverify(
            role_id="role-1", project_id="project-a",
            conditions="branch main; deps qwenpaw@502b59d; structure plugins/agent-line",
        )
        self.assertEqual(unchanged, [])
        expired = self.store.reverify(
            role_id="role-1", project_id="project-a",
            conditions="branch feature; deps qwenpaw@502b59d; structure plugins/agent-line",
        )
        self.assertCountEqual(expired, ["exp-old", "exp-bound"])
        self.assertEqual(self.store.get("role-1", "project-a", "exp-old")["state"], "expired")
        kept = self.store.recall(
            role_id="role-1", project_id="project-a",
            hits=[{"text": self.old}, {"text": "bound constraint"}, {"text": "native unmarked"}],
        )
        self.assertEqual([hit["text"] for hit in kept], ["native unmarked"])

    def test_legacy_ledger_gains_condition_columns_without_hiding_old_rows(self):
        legacy = Path(self.tmp.name) / "legacy.db"
        import os, sqlite3
        fd = os.open(legacy, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(legacy) as db:
            db.execute(
                "CREATE TABLE applicability (role_id TEXT NOT NULL, project_id TEXT NOT NULL, "
                "experience_id TEXT NOT NULL, content_sha256 TEXT NOT NULL, state TEXT NOT NULL, "
                "supersedes TEXT, superseded_by TEXT, reason TEXT NOT NULL, version INTEGER NOT NULL, "
                "updated_at REAL NOT NULL, PRIMARY KEY(role_id, project_id, experience_id))"
            )
            db.execute(
                "INSERT INTO applicability VALUES('role-1','project-a','exp-legacy',?, 'active', NULL, NULL, 'reviewed', 1, 1)",
                (digest("legacy fact"),),
            )
        store = MemoryApplicability(legacy)
        self.assertEqual(store.get("role-1", "project-a", "exp-legacy")["fact_class"], "durable")
        self.assertIsNone(store.get("role-1", "project-a", "exp-legacy")["conditions"])
        self.assertEqual(store.reverify(role_id="role-1", project_id="project-a", conditions="branch main"), [])
        kept = store.recall(role_id="role-1", project_id="project-a", hits=[{"text": "legacy fact"}])
        self.assertEqual(len(kept), 1)


class ApplicabilityApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MemoryApplicability.initialize(Path(self.tmp.name) / "applicability.db")
        self.roles = {"role-1": "project-a"}
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(build_applicability_router(self.store, self.roles))
        self.client = TestClient(app)

    def test_classify_is_extension_ledger_and_rejects_other_projects(self):
        body = {
            "roleId": "role-1", "projectId": "project-a", "experienceId": "exp-1",
            "contentSha256": digest("formal constraint"), "state": "active", "reason": "reviewed",
        }
        created = self.client.post("/memory-applicability/classifications", json=body)
        self.assertEqual(created.status_code, 200, created.text)
        self.assertFalse(created.json()["nativeSupport"])
        self.assertEqual(created.json()["authority"], "extension-ledger")
        self.assertEqual(created.json()["state"], "active")
        read = self.client.get("/memory-applicability/classifications/role-1/project-a/exp-1")
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json()["contentSha256"], body["contentSha256"])
        foreign = dict(body, projectId="project-b", experienceId="exp-2")
        self.assertEqual(self.client.post("/memory-applicability/classifications", json=foreign).status_code, 404)
        status = self.client.get("/memory-applicability")
        self.assertEqual(status.json()["enabled"], True)
        self.assertEqual(status.json()["roles"], ["role-1"])
        self.assertFalse(status.json()["nativeSupport"])

        listed = self.client.get("/memory-applicability/classifications/role-1/project-a")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["classifications"][0]["experienceId"], "exp-1")
        changed = self.client.post("/memory-applicability/reverify", json={
            "roleId": "role-1", "projectId": "project-a", "conditions": "branch other",
        })
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()["expired"], [])

    def test_disabled_router_does_not_classify(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(build_applicability_router(None, {}))
        client = TestClient(app)
        self.assertEqual(client.get("/memory-applicability").json()["enabled"], False)
        self.assertEqual(client.post("/memory-applicability/classifications", json={
            "roleId": "role-1", "projectId": "project-a", "experienceId": "exp-1",
            "contentSha256": digest("formal constraint"), "state": "active", "reason": "reviewed",
        }).status_code, 404)
