"""Extension metadata for experience applicability.

QwenPaw/ReMe does not expose candidate, active, superseded, expired, rejected
and deleted as native states. This ledger is the product record. It never
claims a native search hit is one of those states unless this ledger says so.
Unclassified hits stay visible. Recall drops classified hits that are not
active, except an explicitly requested unverified view of candidates.
Exact-message hiding matches a whole body only; a fragment is not recognized.
"""
import hashlib
import os
import re
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path

from .store import Conflict

STATES = ("candidate", "active", "superseded", "expired", "rejected", "deleted")
TERMINAL = frozenset(("superseded", "expired", "rejected", "deleted"))
FACT_CLASSES = ("durable", "test_status", "deployment_status", "permission", "token_validity")
VOLATILE_FACTS = frozenset(FACT_CLASSES) - {"durable"}
SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
SCHEMA = """
CREATE TABLE applicability (
    role_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    experience_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL,
    supersedes TEXT,
    superseded_by TEXT,
    reason TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    conditions TEXT,
    fact_class TEXT NOT NULL DEFAULT 'durable',
    PRIMARY KEY(role_id, project_id, experience_id)
);
"""


def _segment(value, label):
    if not isinstance(value, str) or not SEGMENT.fullmatch(value) or ".." in value:
        raise ValueError(label)
    return value


def _digest(value):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ValueError("content_sha256_invalid")
    return value


def _hash_text(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("content_invalid")
    return hashlib.sha256(text.strip().encode()).hexdigest()


def _fact_class(value):
    if value not in FACT_CLASSES:
        raise ValueError("fact_class_invalid")
    return value


def _conditions(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or "\n" in value:
        raise ValueError("conditions_invalid")
    return value.strip()


class MemoryApplicability:
    available = True

    def __init__(self, path):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise ValueError("Applicability database missing; explicit initialization required")
        with closing(self._connect()) as db:
            self._ensure_columns(db)
            db.commit()

    @classmethod
    def initialize(cls, path):
        path = Path(path).resolve()
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        return cls(path)

    def _connect(self):
        info = self.path.stat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            raise ValueError("Applicability database must be owner-held mode 0600")
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _ensure_columns(db):
        columns = {row[1] for row in db.execute("PRAGMA table_info(applicability)")}
        if "conditions" not in columns:
            db.execute("ALTER TABLE applicability ADD COLUMN conditions TEXT")
        if "fact_class" not in columns:
            db.execute("ALTER TABLE applicability ADD COLUMN fact_class TEXT NOT NULL DEFAULT 'durable'")

    def get(self, role_id, project_id, experience_id):
        role_id = _segment(role_id, "role_invalid")
        project_id = _segment(project_id, "project_invalid")
        experience_id = _segment(experience_id, "experience_invalid")
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM applicability WHERE role_id=? AND project_id=? AND experience_id=?",
                (role_id, project_id, experience_id),
            ).fetchone()
        if row is None:
            raise KeyError((role_id, project_id, experience_id))
        return dict(row)

    def classify(self, *, role_id, project_id, experience_id, content_sha256, state, supersedes=None, reason,
                 fact_class="durable", conditions=None):
        role_id = _segment(role_id, "role_invalid")
        project_id = _segment(project_id, "project_invalid")
        experience_id = _segment(experience_id, "experience_invalid")
        content_sha256 = _digest(content_sha256)
        fact_class = _fact_class(fact_class)
        conditions = _conditions(conditions)
        if state not in STATES:
            raise ValueError("applicability_state_invalid")
        if fact_class in VOLATILE_FACTS and state == "active":
            raise ValueError("volatile_fact_cannot_be_active")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
            raise ValueError("applicability_reason_invalid")
        if supersedes is not None:
            supersedes = _segment(supersedes, "supersedes_invalid")
            if state != "active" or supersedes == experience_id:
                raise ValueError("supersede_requires_new_active_experience")
        with closing(self._connect()) as db:
            self._ensure_columns(db)
            db.execute("BEGIN IMMEDIATE")
            try:
                current = db.execute(
                    "SELECT * FROM applicability WHERE role_id=? AND project_id=? AND experience_id=?",
                    (role_id, project_id, experience_id),
                ).fetchone()
                if current is not None:
                    if current["content_sha256"] != content_sha256 or current["state"] in TERMINAL:
                        raise Conflict("classified_experience_is_immutable")
                    same = (current["state"] == state and current["supersedes"] == supersedes
                            and current["fact_class"] == fact_class and current["conditions"] == conditions)
                    if same:
                        db.commit()
                        return dict(current)
                    metadata_edit = current["state"] == state and current["supersedes"] == supersedes
                    allowed = metadata_edit or (
                        current["state"] == "candidate" and state == "active" and supersedes is None
                    ) or (
                        current["state"] in ("candidate", "active") and state in ("rejected", "expired", "deleted")
                        and supersedes is None
                    )
                    if not allowed:
                        raise Conflict("applicability_transition_rejected")
                predecessor = None
                if supersedes is not None:
                    predecessor = db.execute(
                        "SELECT * FROM applicability WHERE role_id=? AND project_id=? AND experience_id=?",
                        (role_id, project_id, supersedes),
                    ).fetchone()
                    if predecessor is None or predecessor["state"] not in ("active", "candidate"):
                        raise Conflict("predecessor_not_replaceable")
                now = time.time()
                if current is None:
                    db.execute(
                        "INSERT INTO applicability(role_id,project_id,experience_id,content_sha256,state,supersedes,superseded_by,reason,version,updated_at,conditions,fact_class) "
                        "VALUES(?,?,?,?,?,?,?,?,1,?,?,?)",
                        (role_id, project_id, experience_id, content_sha256, state, supersedes, None, reason.strip(), now, conditions, fact_class),
                    )
                else:
                    db.execute(
                        "UPDATE applicability SET state=?, reason=?, version=version+1, updated_at=?, conditions=?, fact_class=? "
                        "WHERE role_id=? AND project_id=? AND experience_id=?",
                        (state, reason.strip(), now, conditions, fact_class, role_id, project_id, experience_id),
                    )
                if predecessor is not None:
                    db.execute(
                        "UPDATE applicability SET state='superseded', superseded_by=?, reason=?, version=version+1, updated_at=? "
                        "WHERE role_id=? AND project_id=? AND experience_id=?",
                        (experience_id, "superseded_by:" + experience_id, now, role_id, project_id, supersedes),
                    )
                row = db.execute(
                    "SELECT * FROM applicability WHERE role_id=? AND project_id=? AND experience_id=?",
                    (role_id, project_id, experience_id),
                ).fetchone()
                db.commit()
                return dict(row)
            except Exception:
                db.rollback()
                raise

    def list_for(self, role_id, project_id):
        role_id = _segment(role_id, "role_invalid")
        project_id = _segment(project_id, "project_invalid")
        with closing(self._connect()) as db:
            self._ensure_columns(db)
            rows = db.execute(
                "SELECT * FROM applicability WHERE role_id=? AND project_id=? ORDER BY experience_id",
                (role_id, project_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def reverify(self, *, role_id, project_id, conditions):
        """Expire active memories whose declared code conditions no longer match.

        A memory with no conditions does not depend on branch, dependency, or
        structure, so a later change does not hide it. Expired rows stay
        readable and are not restored.
        """
        role_id = _segment(role_id, "role_invalid")
        project_id = _segment(project_id, "project_invalid")
        conditions = _conditions(conditions)
        if conditions is None:
            raise ValueError("conditions_invalid")
        now = time.time()
        with closing(self._connect()) as db:
            self._ensure_columns(db)
            db.execute("BEGIN IMMEDIATE")
            try:
                rows = db.execute(
                    "SELECT experience_id FROM applicability WHERE role_id=? AND project_id=? AND state='active' "
                    "AND conditions IS NOT NULL AND conditions<>?",
                    (role_id, project_id, conditions),
                ).fetchall()
                ids = [row["experience_id"] for row in rows]
                if ids:
                    db.execute(
                        "UPDATE applicability SET state='expired', reason=?, version=version+1, updated_at=? "
                        "WHERE role_id=? AND project_id=? AND state='active' AND conditions IS NOT NULL AND conditions<>?",
                        ("condition_changed", now, role_id, project_id, conditions),
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return ids

    def recall(self, *, role_id, project_id, hits, include_unverified=False):
        role_id = _segment(role_id, "role_invalid")
        project_id = _segment(project_id, "project_invalid")
        if not isinstance(hits, list):
            raise ValueError("recall_hits_invalid")
        kept = []
        with closing(self._connect()) as db:
            for hit in hits:
                if not isinstance(hit, dict):
                    raise ValueError("recall_hit_invalid")
                decision = self._decide(db, role_id, project_id, hit, include_unverified)
                if decision is not None:
                    kept.append(decision)
        return kept

    def exact_hidden(self, role_id, project_id, text):
        try:
            digest = _hash_text(text)
            role_id = _segment(role_id, "role_invalid")
            project_id = _segment(project_id, "project_invalid")
        except ValueError:
            return False
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT state FROM applicability WHERE role_id=? AND project_id=? AND content_sha256=?",
                (role_id, project_id, digest),
            ).fetchall()
        if len(rows) != 1:
            return False
        return rows[0]["state"] != "active"

    def _decide(self, db, role_id, project_id, hit, include_unverified):
        experience_id = hit.get("experienceId")
        digest = hit.get("contentSha256")
        text = hit.get("text")
        if experience_id is not None:
            experience_id = _segment(experience_id, "experience_invalid")
        if digest is not None:
            digest = _digest(digest)
        if text is not None:
            text_digest = _hash_text(text)
            if digest is not None and digest != text_digest:
                return None
            digest = text_digest
        if experience_id is not None:
            row = db.execute(
                "SELECT * FROM applicability WHERE role_id=? AND project_id=? AND experience_id=?",
                (role_id, project_id, experience_id),
            ).fetchone()
            if row is None:
                if _foreign(db, project_id, experience_id=experience_id, digest=digest):
                    return None
                return self._unclassified(hit)
            if digest is not None and row["content_sha256"] != digest:
                return None
        elif digest is not None:
            rows = db.execute(
                "SELECT * FROM applicability WHERE role_id=? AND project_id=? AND content_sha256=?",
                (role_id, project_id, digest),
            ).fetchall()
            if not rows:
                if _foreign(db, project_id, digest=digest):
                    return None
                return self._unclassified(hit)
            if len(rows) != 1:
                return None
            row = rows[0]
        else:
            raise ValueError("recall_hit_identity_missing")
        if row["state"] == "active":
            return self._visible(hit, row, "正式")
        if row["state"] == "candidate" and include_unverified:
            return self._visible(hit, row, "待验证")
        return None

    @staticmethod
    def _unclassified(hit):
        return {**hit, "applicability": "unclassified", "label": None}

    @staticmethod
    def _visible(hit, row, label):
        return {
            **hit,
            "experienceId": row["experience_id"],
            "contentSha256": row["content_sha256"],
            "applicability": row["state"],
            "label": label,
            "supersedes": row["supersedes"],
            "supersededBy": row["superseded_by"],
        }



def _foreign(db, project_id, *, experience_id=None, digest=None):
    """Another project's classified id or whole-body hash is not reusable here."""
    if experience_id is not None:
        found = db.execute(
            "SELECT 1 FROM applicability WHERE experience_id=? AND project_id<>? LIMIT 1",
            (experience_id, project_id),
        ).fetchone()
        if found is not None:
            return True
    if digest is not None:
        found = db.execute(
            "SELECT 1 FROM applicability WHERE content_sha256=? AND project_id<>? LIMIT 1",
            (digest, project_id),
        ).fetchone()
        if found is not None:
            return True
    return False


NO_MEMORY_RESULTS = "(no memory results)"


def _search_sections(text):
    """Split a ReMe search answer. A preamble means the whole text is one body."""
    if not text.startswith("=========="):
        return None
    sections = []
    header = None
    body = []
    for line in text.split("\n"):
        if line.startswith("=========="):
            if header is not None:
                sections.append((header, "\n".join(body)))
            header = line
            body = []
        elif header is None:
            return None
        else:
            body.append(line)
    if header is None:
        return None
    sections.append((header, "\n".join(body)))
    return sections


def _whole_section_text(text):
    """Drop blank-only lines a renderer inserts without changing the section."""
    if not isinstance(text, str):
        return text
    return "\n".join(line for line in text.strip().split("\n") if line.strip())


def filter_search_text(store, role_id, project_id, text, *, include_unverified=False):
    """Drop classified hits a managed memory_search result is not allowed to show.

    The native answer is not a native six-state record. A whole section body or
    a whole unstructured result is matched. Blank-only lines inserted by the
    renderer do not make that body a different section. A fragment or paraphrase
    stays. Unclassified text stays. Stored hashes and exact message hiding stay
    exact. The original text is returned unchanged when every section remains
    visible.
    """
    if store is None or not isinstance(text, str) or not text.strip():
        return text
    sections = _search_sections(text)
    if sections is None:
        kept = store.recall(
            role_id=role_id, project_id=project_id, hits=[{"text": _whole_section_text(text)}],
            include_unverified=include_unverified,
        )
        return text if kept else NO_MEMORY_RESULTS
    visible = []
    for header, body in sections:
        probe = body if body.strip() else header
        kept = store.recall(
            role_id=role_id, project_id=project_id, hits=[{"text": _whole_section_text(probe)}],
            include_unverified=include_unverified,
        )
        if kept:
            visible.append(header if body == "" else header + "\n" + body)
    if len(visible) == len(sections):
        return text
    if not visible:
        return NO_MEMORY_RESULTS
    return "\n".join(visible)


def validate_role_projects(value):
    if not isinstance(value, dict) or not value:
        raise ValueError("applicability_roles_invalid")
    mapping = {}
    for role_id, project_id in value.items():
        mapping[_segment(role_id, "role_invalid")] = _segment(project_id, "project_invalid")
    return mapping


def public_classification(row):
    return {
        "roleId": row["role_id"],
        "projectId": row["project_id"],
        "experienceId": row["experience_id"],
        "contentSha256": row["content_sha256"],
        "state": row["state"],
        "supersedes": row["supersedes"],
        "supersededBy": row["superseded_by"],
        "reason": row["reason"],
        "version": row["version"],
        "factClass": row["fact_class"] if "fact_class" in row.keys() else "durable",
        "conditions": row["conditions"] if "conditions" in row.keys() else None,
        "nativeSupport": False,
        "authority": "extension-ledger",
    }


def build_applicability_router(store, role_projects, *, broken=False):
    """Formal extension ledger API. It does not claim QwenPaw or ReMe states."""
    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel, ConfigDict, Field

    class ClassifyRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        roleId: str = Field(min_length=1, max_length=160)
        projectId: str = Field(min_length=1, max_length=160)
        experienceId: str = Field(min_length=1, max_length=160)
        contentSha256: str = Field(pattern=r"^[a-f0-9]{64}$")
        state: str = Field(min_length=1, max_length=32)
        reason: str = Field(min_length=1, max_length=256)
        supersedes: str | None = Field(default=None, min_length=1, max_length=160)
        factClass: str = Field(default="durable", min_length=1, max_length=32)
        conditions: str | None = Field(default=None, min_length=1, max_length=512)

    class ReverifyRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        roleId: str = Field(min_length=1, max_length=160)
        projectId: str = Field(min_length=1, max_length=160)
        conditions: str = Field(min_length=1, max_length=512)

    router = APIRouter()

    def status():
        enabled = store is not None and not broken
        return {
            "enabled": enabled,
            "degraded": bool(broken),
            "nativeSupport": False,
            "authority": "extension-ledger",
            "roles": sorted(role_projects) if enabled else [],
        }

    @router.get("/memory-applicability")
    def describe():
        return status()

    def bound(role_id, project_id):
        if broken or store is None:
            raise HTTPException(503 if broken else 404, "适用性账本未启用")
        expected = role_projects.get(role_id)
        if expected is None or expected != project_id:
            raise HTTPException(404, "适用性角色不存在")
        return expected

    @router.post("/memory-applicability/classifications")
    def classify(body: ClassifyRequest):
        bound(body.roleId, body.projectId)
        try:
            row = store.classify(
                role_id=body.roleId, project_id=body.projectId, experience_id=body.experienceId,
                content_sha256=body.contentSha256, state=body.state, supersedes=body.supersedes,
                reason=body.reason, fact_class=body.factClass, conditions=body.conditions,
            )
        except ValueError:
            raise HTTPException(422, "适用性分类无效") from None
        except Exception as exc:
            if exc.__class__.__name__ == "Conflict":
                raise HTTPException(409, "适用性分类被拒绝") from None
            raise
        return public_classification(row)

    @router.get("/memory-applicability/classifications/{role_id}/{project_id}/{experience_id}")
    def read(role_id: str, project_id: str, experience_id: str):
        bound(role_id, project_id)
        try:
            row = store.get(role_id, project_id, experience_id)
        except KeyError:
            raise HTTPException(404, "适用性记录不存在") from None
        except ValueError:
            raise HTTPException(422, "适用性分类无效") from None
        return public_classification(row)

    @router.get("/memory-applicability/classifications/{role_id}/{project_id}")
    def listed(role_id: str, project_id: str):
        bound(role_id, project_id)
        try:
            rows = store.list_for(role_id, project_id)
        except ValueError:
            raise HTTPException(422, "适用性分类无效") from None
        return {"nativeSupport": False, "authority": "extension-ledger",
                "classifications": [public_classification(row) for row in rows]}

    @router.post("/memory-applicability/reverify")
    def reverify(body: ReverifyRequest):
        bound(body.roleId, body.projectId)
        try:
            expired = store.reverify(role_id=body.roleId, project_id=body.projectId, conditions=body.conditions)
        except ValueError:
            raise HTTPException(422, "适用性分类无效") from None
        return {"nativeSupport": False, "authority": "extension-ledger", "expired": expired}

    return router


def drop_exact_hidden(store, role_id, project_id, messages, text_of):
    """Drop a message only when its whole text is one classified non-active body."""
    if store is None or not role_id or not project_id or not isinstance(messages, list):
        return messages
    kept = []
    for message in messages:
        text = text_of(message)
        if isinstance(text, str) and text.strip() and store.exact_hidden(role_id, project_id, text):
            continue
        kept.append(message)
    return kept
