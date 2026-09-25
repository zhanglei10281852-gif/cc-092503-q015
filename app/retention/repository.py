"""保存策略、法律保留、论文复核、保留延期与评估结论的数据访问层。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.errors import ConflictError, NotFoundError


def _row(row: sqlite3.Row | None, message: str) -> dict[str, Any]:
    if row is None:
        raise NotFoundError(message)
    return dict(row)


class RetentionPolicyRepository:
    """版本化保存策略：任何修改都新增版本，旧版本只标记 superseded，绝不改写。"""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def list_versions(self, scope_type: str | None = None, scope_value: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM retention_policy_versions"
        clauses: list[str] = []
        params: list[Any] = []
        if scope_type:
            clauses.append("scope_type=?")
            params.append(scope_type)
        if scope_value is not None:
            clauses.append("scope_value=?")
            params.append(scope_value)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY scope_type,scope_value,version DESC"
        return [dict(row) for row in self.connection.execute(sql, tuple(params)).fetchall()]

    def active_policies(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM retention_policy_versions WHERE status='active' ORDER BY scope_type,scope_value"
            ).fetchall()
        ]

    def get(self, policy_id: int) -> dict[str, Any]:
        return _row(
            self.connection.execute(
                "SELECT * FROM retention_policy_versions WHERE id=?", (policy_id,)
            ).fetchone(),
            "保存策略版本不存在",
        )

    def active_for(self, scope_type: str, scope_value: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM retention_policy_versions WHERE scope_type=? AND scope_value=? AND status='active'",
            (scope_type, scope_value),
        ).fetchone()
        return dict(row) if row else None

    def next_version(self, scope_type: str, scope_value: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM retention_policy_versions WHERE scope_type=? AND scope_value=?",
            (scope_type, scope_value),
        ).fetchone()
        return int(row[0]) + 1

    def create_version(
        self,
        *,
        scope_type: str,
        scope_value: str,
        retain_days: int,
        legal_hold_days: int,
        basis_text: str,
        change_reason: str,
        created_by: int | None,
        now: str,
    ) -> dict[str, Any]:
        """新增策略版本并把同作用域的旧生效版本标记为 superseded（不改写历史行）。"""
        version = self.next_version(scope_type, scope_value)
        # 先解除旧生效版本占用的 active 唯一槽位，再写入新版本
        self.connection.execute(
            """UPDATE retention_policy_versions
               SET status='superseded',superseded_at=?
               WHERE scope_type=? AND scope_value=? AND status='active'""",
            (now, scope_type, scope_value),
        )
        cursor = self.connection.execute(
            """INSERT INTO retention_policy_versions(
                   version,scope_type,scope_value,retain_days,legal_hold_days,basis_text,
                   change_reason,status,created_by,created_at
               ) VALUES(?,?,?,?,?,?,?,'active',?,?)""",
            (version, scope_type, scope_value, retain_days, legal_hold_days, basis_text,
             change_reason, created_by, now),
        )
        new_id = int(cursor.lastrowid)
        self.connection.execute(
            "UPDATE retention_policy_versions SET superseded_by=? WHERE scope_type=? AND scope_value=? AND status='superseded' AND superseded_by IS NULL",
            (new_id, scope_type, scope_value),
        )
        return self.get(new_id)


class LegalHoldRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, data: dict[str, Any], created_by: int, hold_code: str, starts_at: str, expires_at: str | None, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO legal_holds(
                   hold_code,scope_type,sample_id,scope_value,reason,hold_days,state,
                   created_by,starts_at,expires_at,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,'active',?,?,?,?,?)""",
            (
                hold_code, data["scope_type"], data.get("sample_id"), data.get("scope_value", ""),
                data["reason"], data.get("hold_days"), created_by, starts_at, expires_at, now, now,
            ),
        )
        return self.get(int(cursor.lastrowid))

    def get(self, hold_id: int) -> dict[str, Any]:
        return _row(
            self.connection.execute("SELECT * FROM legal_holds WHERE id=?", (hold_id,)).fetchone(),
            "法律保留记录不存在",
        )

    def by_code(self, hold_code: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM legal_holds WHERE hold_code=?", (hold_code,)).fetchone()
        return dict(row) if row else None

    def list(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM legal_holds"
        if active_only:
            sql += " WHERE state='active'"
        sql += " ORDER BY id DESC"
        return [dict(row) for row in self.connection.execute(sql).fetchall()]

    def active_for_sample(self, sample_id: int, *, project_code: str, sample_type: str, now: str) -> list[dict[str, Any]]:
        """命中样品、项目或样品类型作用域，且当前处于有效期内的法律保留。"""
        rows = self.connection.execute(
            """SELECT * FROM legal_holds
               WHERE state='active' AND starts_at<=? AND (expires_at IS NULL OR expires_at>?)
                 AND (
                   (scope_type='sample' AND sample_id=?)
                   OR (scope_type='project' AND scope_value=?)
                   OR (scope_type='sample_type' AND scope_value=?)
                 )
               ORDER BY id""",
            (now, now, sample_id, project_code, sample_type),
        ).fetchall()
        return [dict(row) for row in rows]

    def release(self, hold_id: int, released_by: int, reason: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """UPDATE legal_holds SET state='released',released_at=?,released_by=?,release_reason=?,updated_at=?
               WHERE id=? AND state='active'""",
            (now, released_by, reason, now, hold_id),
        )
        if cursor.rowcount != 1:
            raise ConflictError("法律保留不存在或已解除")
        return self.get(hold_id)


class PublicationReviewRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, data: dict[str, Any], registered_by: int, now: str) -> dict[str, Any]:
        try:
            cursor = self.connection.execute(
                """INSERT INTO publication_reviews(
                       sample_id,publication_code,title,review_state,expected_clear_at,
                       registered_by,note,created_at,updated_at
                   ) VALUES(?,?,?,'under_review',?,?,?,?,?)""",
                (
                    data["sample_id"], data["publication_code"], data.get("title", ""),
                    data.get("expected_clear_at"), registered_by, data.get("note", ""), now, now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该论文复核引用已登记") from exc
        return self.get(int(cursor.lastrowid))

    def get(self, review_id: int) -> dict[str, Any]:
        return _row(
            self.connection.execute("SELECT * FROM publication_reviews WHERE id=?", (review_id,)).fetchone(),
            "论文复核引用不存在",
        )

    def list_for_sample(self, sample_id: int, *, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM publication_reviews WHERE sample_id=?"
        if active_only:
            sql += " AND review_state='under_review'"
        sql += " ORDER BY id"
        return [dict(row) for row in self.connection.execute(sql, (sample_id,)).fetchall()]

    def transition(self, review_id: int, review_state: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "UPDATE publication_reviews SET review_state=?,updated_at=? WHERE id=?",
            (review_state, now, review_id),
        )
        if cursor.rowcount != 1:
            raise ConflictError("论文复核引用不存在或状态已变化")
        return self.get(review_id)

    def active_for_sample(self, sample_id: int, *, now: str) -> list[dict[str, Any]]:
        """仍在复核中，且没有明确预计清除时间，或预计清除时间尚未来到。"""
        rows = self.connection.execute(
            """SELECT * FROM publication_reviews
               WHERE sample_id=? AND review_state='under_review'
                 AND (expected_clear_at IS NULL OR expected_clear_at>?)
               ORDER BY id""",
            (sample_id, now),
        ).fetchall()
        return [dict(row) for row in rows]


class RetentionExtensionRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, data: dict[str, Any], requested_by: int, extension_code: str, expires_at: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO retention_extensions(
                   extension_code,sample_id,reason,extra_days,requested_by,state,
                   required_approvals,expires_at,created_at,updated_at
               ) VALUES(?,?,?,?,?,'pending',2,?,?,?)""",
            (
                extension_code, data["sample_id"], data["reason"], data["extra_days"],
                requested_by, expires_at, now, now,
            ),
        )
        return self.get(int(cursor.lastrowid))

    def get(self, extension_id: int) -> dict[str, Any]:
        row = _row(
            self.connection.execute("SELECT * FROM retention_extensions WHERE id=?", (extension_id,)).fetchone(),
            "保留延期申请不存在",
        )
        row["decisions"] = [
            dict(item)
            for item in self.connection.execute(
                "SELECT * FROM retention_extension_decisions WHERE extension_id=? ORDER BY id",
                (extension_id,),
            ).fetchall()
        ]
        return row

    def decide(self, extension_id: int, approver_user_id: int, decision: str, comment: str, now: str) -> dict[str, Any]:
        extension = self.get(extension_id)
        if extension["state"] != "pending":
            raise ConflictError("延期申请已经结束")
        if extension["requested_by"] == approver_user_id:
            raise ConflictError("申请人不能审批自己的延期申请")
        if now > extension["expires_at"]:
            self.connection.execute(
                "UPDATE retention_extensions SET state='expired',decided_at=?,version=version+1,updated_at=? WHERE id=?",
                (now, now, extension_id),
            )
            raise ConflictError("延期申请已超过审批时限")
        try:
            self.connection.execute(
                """INSERT INTO retention_extension_decisions(extension_id,approver_user_id,decision,comment,decided_at)
                   VALUES(?,?,?,?,?)""",
                (extension_id, approver_user_id, decision, comment, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该审批人已经对本延期申请作出决定") from exc
        decisions = self.connection.execute(
            "SELECT decision FROM retention_extension_decisions WHERE extension_id=?", (extension_id,)
        ).fetchall()
        if any(item[0] == "reject" for item in decisions):
            state = "rejected"
        elif len(decisions) >= extension["required_approvals"]:
            state = "approved"
        else:
            state = "pending"
        self.connection.execute(
            "UPDATE retention_extensions SET state=?,decided_at=CASE WHEN ?='pending' THEN NULL ELSE ? END,version=version+1,updated_at=? WHERE id=?",
            (state, state, now, now, extension_id),
        )
        return self.get(extension_id)

    def approved_for_sample(self, sample_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM retention_extensions WHERE sample_id=? AND state='approved' ORDER BY id",
                (sample_id,),
            ).fetchall()
        ]

    def list(self, *, state: str | None = None, sample_id: int | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if state:
            clauses.append("e.state=?")
            params.append(state)
        if sample_id:
            clauses.append("e.sample_id=?")
            params.append(sample_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(100)
        rows = self.connection.execute(
            f"""SELECT e.*,u.display_name AS requester_name,s.sample_code
                FROM retention_extensions e
                JOIN users u ON u.id=e.requested_by
                JOIN samples s ON s.id=e.sample_id{where}
                ORDER BY e.id DESC LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]


class EvaluationRepository:
    """评估运行记录与不可变结论行；结论失效只置 stale=1，绝不更新或删除原结论。"""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def get_by_date(self, evaluation_date: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM retention_evaluations WHERE evaluation_date=?", (evaluation_date,)
        ).fetchone()
        return dict(row) if row else None

    def get(self, evaluation_id: int) -> dict[str, Any]:
        return _row(
            self.connection.execute("SELECT * FROM retention_evaluations WHERE id=?", (evaluation_id,)).fetchone(),
            "到期评估不存在",
        )

    def create(self, evaluation_date: str, policy_fingerprint: str, total_samples: int, started_by: str, now: str) -> dict[str, Any]:
        try:
            cursor = self.connection.execute(
                """INSERT INTO retention_evaluations(
                       evaluation_date,status,policy_fingerprint,total_samples,started_at,started_by,created_at,updated_at
                   ) VALUES(?,'running',?,?,?,?,?,?)""",
                (evaluation_date, policy_fingerprint, total_samples, now, started_by, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该日期的到期评估已存在") from exc
        return self.get(int(cursor.lastrowid))

    def mark_failed(self, evaluation_id: int, message: str, now: str) -> None:
        self.connection.execute(
            "UPDATE retention_evaluations SET status='failed',error_message=?,updated_at=? WHERE id=?",
            (message[:1000], now, evaluation_id),
        )

    def save_checkpoint(self, evaluation_id: int, checkpoint: dict[str, Any], counters: dict[str, int], now: str) -> None:
        self.connection.execute(
            """UPDATE retention_evaluations
               SET checkpoint_json=?,processed_samples=?,included_count=?,excluded_count=?,
                   deferred_count=?,updated_at=?
               WHERE id=?""",
            (
                json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
                counters["processed"], counters["included"], counters["excluded"], counters["deferred"],
                now, evaluation_id,
            ),
        )

    def complete(self, evaluation_id: int, checkpoint: dict[str, Any], counters: dict[str, int], now: str) -> None:
        self.connection.execute(
            """UPDATE retention_evaluations
               SET status='completed',checkpoint_json=?,processed_samples=?,included_count=?,
                   excluded_count=?,deferred_count=?,completed_at=?,updated_at=?
               WHERE id=?""",
            (
                json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
                counters["processed"], counters["included"], counters["excluded"], counters["deferred"],
                now, now, evaluation_id,
            ),
        )

    def checkpoint(self, evaluation_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT checkpoint_json FROM retention_evaluations WHERE id=?", (evaluation_id,)
        ).fetchone()
        return json.loads(row[0]) if row and row[0] else {}

    def list_runs(self, *, limit: int = 60) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM retention_evaluations ORDER BY evaluation_date DESC,id DESC LIMIT ?", (limit,)
            ).fetchall()
        ]

    def upsert_conclusion(self, evaluation_id: int, conclusion: dict[str, Any]) -> None:
        """评估内每样品一行；检查点恢复时覆盖同一评估内未完成写入，历史评估的行永不被改写。"""
        self.connection.execute(
            """INSERT INTO retention_conclusions(
                   evaluation_id,sample_id,outcome,reason_code,reason_detail,policy_version_id,
                   base_retention_date,eligible_at,deferred_until,applied_extension_ids_json,
                   blocking_hold_ids_json,blocking_loan_ids_json,blocking_anomaly_ids_json,
                   blocking_review_ids_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(evaluation_id,sample_id) DO UPDATE SET
                   outcome=excluded.outcome,
                   reason_code=excluded.reason_code,
                   reason_detail=excluded.reason_detail,
                   policy_version_id=excluded.policy_version_id,
                   base_retention_date=excluded.base_retention_date,
                   eligible_at=excluded.eligible_at,
                   deferred_until=excluded.deferred_until,
                   applied_extension_ids_json=excluded.applied_extension_ids_json,
                   blocking_hold_ids_json=excluded.blocking_hold_ids_json,
                   blocking_loan_ids_json=excluded.blocking_loan_ids_json,
                   blocking_anomaly_ids_json=excluded.blocking_anomaly_ids_json,
                   blocking_review_ids_json=excluded.blocking_review_ids_json""",
            (
                evaluation_id,
                conclusion["sample_id"],
                conclusion["outcome"],
                conclusion["reason_code"],
                conclusion.get("reason_detail", ""),
                conclusion.get("policy_version_id"),
                conclusion.get("base_retention_date"),
                conclusion.get("eligible_at"),
                conclusion.get("deferred_until"),
                json.dumps(conclusion.get("applied_extension_ids", []), ensure_ascii=False),
                json.dumps(conclusion.get("blocking_hold_ids", []), ensure_ascii=False),
                json.dumps(conclusion.get("blocking_loan_ids", []), ensure_ascii=False),
                json.dumps(conclusion.get("blocking_anomaly_ids", []), ensure_ascii=False),
                json.dumps(conclusion.get("blocking_review_ids", []), ensure_ascii=False),
                conclusion["created_at"],
            ),
        )

    def conclusion_processed_sample_ids(self, evaluation_id: int) -> set[int]:
        return {
            int(row[0])
            for row in self.connection.execute(
                "SELECT sample_id FROM retention_conclusions WHERE evaluation_id=?", (evaluation_id,)
            ).fetchall()
        }

    def conclusions(self, evaluation_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT c.*,s.sample_code,s.sample_type,s.quantity,s.unit,s.lifecycle_state,
                      b.project_code,b.batch_code
               FROM retention_conclusions c
               JOIN samples s ON s.id=c.sample_id
               JOIN receipt_batches b ON b.id=s.batch_id
               WHERE c.evaluation_id=?
               ORDER BY c.outcome,c.id""",
            (evaluation_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key in (
                "applied_extension_ids_json", "blocking_hold_ids_json", "blocking_loan_ids_json",
                "blocking_anomaly_ids_json", "blocking_review_ids_json",
            ):
                item[key[:-5]] = json.loads(item.pop(key))
            result.append(item)
        return result

    def latest_completed(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM retention_evaluations WHERE status='completed' ORDER BY evaluation_date DESC,id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def mark_stale_after_policy_change(self, scope_type: str, scope_value: str, reason: str, now: str, *, policy_version_id: int) -> int:
        """只把"本次被取代的策略版本"所产生的旧结论标记过期，原行保留不改写。

        依据 superseded_by 精确定位，避免波及由更高优先级作用域策略管辖的样品
        （项目约定优先于类型策略）。返回标记行数。
        """
        cursor = self.connection.execute(
            """UPDATE retention_conclusions
               SET stale=1,stale_reason=?,superseded_at=?
               WHERE stale=0 AND policy_version_id IN (
                   SELECT id FROM retention_policy_versions
                   WHERE scope_type=? AND scope_value=? AND superseded_by=?
               )""",
            (reason, now, scope_type, scope_value, policy_version_id),
        )
        return cursor.rowcount


class DestructionBatchRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(
        self,
        *,
        batch_code: str,
        source_evaluation_id: int,
        planned_by: int,
        note: str,
        items: list[dict[str, Any]],
        manifest_digest: str,
        now: str,
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO destruction_batches(
                   batch_code,source_evaluation_id,state,sample_count,planned_by,note,
                   manifest_digest,created_at,updated_at
               ) VALUES(?,?,'planned',?,?,?,?,?,?)""",
            (batch_code, source_evaluation_id, len(items), planned_by, note, manifest_digest, now, now),
        )
        batch_id = int(cursor.lastrowid)
        for item in items:
            self.connection.execute(
                """INSERT INTO destruction_batch_items(
                       batch_id,sample_id,conclusion_id,sample_code,quantity,unit,added_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    batch_id, item["sample_id"], item["conclusion_id"], item["sample_code"],
                    item["quantity"], item["unit"], now,
                ),
            )
        return self.get(batch_id)

    def get(self, batch_id: int) -> dict[str, Any]:
        row = _row(
            self.connection.execute("SELECT * FROM destruction_batches WHERE id=?", (batch_id,)).fetchone(),
            "成组销毁计划不存在",
        )
        row["items"] = [
            dict(item)
            for item in self.connection.execute(
                "SELECT * FROM destruction_batch_items WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        ]
        return row

    def by_code(self, batch_code: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM destruction_batches WHERE batch_code=?", (batch_code,)).fetchone()
        return dict(row) if row else None

    def planned_sample_ids(self) -> set[int]:
        return {
            int(row[0])
            for row in self.connection.execute(
                "SELECT DISTINCT sample_id FROM destruction_batch_items bi "
                "JOIN destruction_batches db ON db.id=bi.batch_id WHERE db.state IN ('planned','released')"
            ).fetchall()
        }

    def list_batches(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM destruction_batches ORDER BY id DESC LIMIT 100"
            ).fetchall()
        ]

    def release(self, batch_id: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "UPDATE destruction_batches SET state='released',released_at=?,updated_at=? WHERE id=? AND state='planned'",
            (now, now, batch_id),
        )
        if cursor.rowcount != 1:
            raise ConflictError("销毁计划不存在或不在待发布状态")
        return self.get(batch_id)
