"""保存策略管理、法律保留、论文复核登记、保留延期审批与成组销毁计划。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.retention.engine import RetentionEvaluationService
from app.retention.repository import (
    DestructionBatchRepository,
    EvaluationRepository,
    LegalHoldRepository,
    PublicationReviewRepository,
    RetentionExtensionRepository,
    RetentionPolicyRepository,
)
from app.services.audit import AuditService

POLICY_SCOPES = {"global", "sample_type", "project"}
HOLD_SCOPES = {"sample", "project", "sample_type"}


class RetentionPolicyService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.policies = RetentionPolicyRepository(connection)
        self.runs = EvaluationRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def create_version(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("retention.policy.write")
        scope_type = data["scope_type"]
        if scope_type not in POLICY_SCOPES:
            raise ValidationError("策略作用域类型无效")
        scope_value = data.get("scope_value", "").strip()
        if scope_type == "global":
            scope_value = ""
        elif not scope_value:
            raise ValidationError("样品类型与项目策略必须填写作用域值")
        now = to_storage(self.clock.now())
        policy = self.policies.create_version(
            scope_type=scope_type,
            scope_value=scope_value,
            retain_days=int(data["retain_days"]),
            legal_hold_days=int(data.get("legal_hold_days", 0)),
            basis_text=data.get("basis_text", "").strip(),
            change_reason=data.get("change_reason", "").strip() or "策略更新",
            created_by=principal.user_id,
            now=now,
        )
        stale_count = self.runs.mark_stale_after_policy_change(
            scope_type,
            scope_value,
            f"保存策略更新至版本 {policy['version']}（{policy['change_reason']}），旧结论标记过期",
            now,
            policy_version_id=policy["id"],
        )
        self.audit.record(
            principal, "retention.policy.version", "retention_policy", str(policy["id"]),
            after=policy, metadata={"stale_conclusions": stale_count},
        )
        return {"policy": policy, "stale_conclusions": stale_count}

    def list_versions(self, principal: Principal, scope_type: str | None, scope_value: str | None) -> list[dict[str, Any]]:
        principal.require("retention.report.read")
        return self.policies.list_versions(scope_type, scope_value)


class LegalHoldService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.holds = LegalHoldRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def create(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("retention.hold.manage")
        scope_type = data["scope_type"]
        if scope_type not in HOLD_SCOPES:
            raise ValidationError("法律保留作用域无效")
        if scope_type == "sample":
            if not data.get("sample_id"):
                raise ValidationError("样品级法律保留必须提供 sample_id")
            if not self.connection.execute("SELECT 1 FROM samples WHERE id=?", (data["sample_id"],)).fetchone():
                raise NotFoundError("样品不存在")
            scope_value = ""
        else:
            scope_value = data.get("scope_value", "").strip()
            if not scope_value:
                raise ValidationError("项目或样品类型法律保留必须填写作用域值")
        if data.get("hold_days") is not None and int(data["hold_days"]) <= 0:
            raise ValidationError("保留天数必须大于 0")
        now_dt = self.clock.now()
        now = to_storage(now_dt)
        hold_code = data.get("hold_code") or f"HOLD-{uuid.uuid4().hex[:12]}"
        if self.holds.by_code(hold_code):
            raise ConflictError("法律保留编号已存在")
        expires_at = to_storage(now_dt + timedelta(days=int(data["hold_days"]))) if data.get("hold_days") else None
        hold = self.holds.create(
            {**data, "scope_value": scope_value}, principal.user_id, hold_code, now, expires_at, now
        )
        self.audit.record(principal, "retention.hold.create", "legal_hold", str(hold["id"]), after=hold)
        return hold

    def release(self, principal: Principal, hold_id: int, reason: str) -> dict[str, Any]:
        principal.require("retention.hold.manage")
        before = self.holds.get(hold_id)
        hold = self.holds.release(hold_id, principal.user_id, reason.strip(), to_storage(self.clock.now()))
        self.audit.record(principal, "retention.hold.release", "legal_hold", str(hold_id), before=before, after=hold)
        return hold

    def list(self, principal: Principal, active_only: bool) -> list[dict[str, Any]]:
        principal.require("retention.report.read")
        return self.holds.list(active_only=active_only)


class PublicationReviewService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.reviews = PublicationReviewRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def register(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("retention.review.register")
        if not self.connection.execute("SELECT 1 FROM samples WHERE id=?", (data["sample_id"],)).fetchone():
            raise NotFoundError("样品不存在")
        review = self.reviews.create(data, principal.user_id, to_storage(self.clock.now()))
        self.audit.record(principal, "retention.review.register", "publication_review", str(review["id"]), after=review)
        return review

    def update_state(self, principal: Principal, review_id: int, review_state: str) -> dict[str, Any]:
        principal.require("retention.review.register")
        if review_state not in {"under_review", "published", "withdrawn"}:
            raise ValidationError("论文复核状态无效")
        before = self.reviews.get(review_id)
        review = self.reviews.transition(review_id, review_state, to_storage(self.clock.now()))
        self.audit.record(principal, "retention.review.update", "publication_review", str(review_id), before=before, after=review)
        return review

    def list_for_sample(self, principal: Principal, sample_id: int) -> list[dict[str, Any]]:
        principal.require("retention.report.read")
        return self.reviews.list_for_sample(sample_id)


class RetentionExtensionService:
    """研究负责人申请有期限的保留延期，经两名不同审批人批准方可生效。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.extensions = RetentionExtensionRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def request(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("retention.extension.request")
        sample = self.connection.execute("SELECT * FROM samples WHERE id=?", (data["sample_id"],)).fetchone()
        if not sample:
            raise NotFoundError("样品不存在")
        if int(data["extra_days"]) <= 0 or int(data["extra_days"]) > 3650:
            raise ValidationError("延期天数必须在 1 到 3650 之间")
        now_dt = self.clock.now()
        now = to_storage(now_dt)
        extension_code = data.get("extension_code") or f"EXT-{uuid.uuid4().hex[:12]}"
        existing = self.connection.execute(
            "SELECT id FROM retention_extensions WHERE extension_code=?", (extension_code,)
        ).fetchone()
        if existing:
            raise ConflictError("延期申请编号已存在")
        extension = self.extensions.create(
            data,
            principal.user_id,
            extension_code,
            to_storage(now_dt + timedelta(days=7)),
            now,
        )
        self.audit.record(principal, "retention.extension.request", "retention_extension", str(extension["id"]), after=extension)
        return extension

    def decide(self, principal: Principal, extension_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("retention.extension.approve")
        before = self.extensions.get(extension_id)
        result = self.extensions.decide(
            extension_id, principal.user_id, data["decision"], data.get("comment", ""), to_storage(self.clock.now())
        )
        self.audit.record(
            principal, "retention.extension.decide", "retention_extension", str(extension_id),
            before={"state": before["state"]}, after={"state": result["state"], "decisions": result["decisions"]},
        )
        return result

    def list(self, principal: Principal, state: str | None, sample_id: int | None) -> list[dict[str, Any]]:
        principal.require("retention.report.read")
        return self.extensions.list(state=state, sample_id=sample_id)


class DestructionPlanningService:
    """把最近一次完成评估的纳入候选安全地转成成组销毁计划；重复提交不会重复建单。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.engine = RetentionEvaluationService(connection, self.clock)
        self.runs = EvaluationRepository(connection)
        self.batches = DestructionBatchRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def plan(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("destruction.plan")
        now = to_storage(self.clock.now())
        run = self.runs.latest_completed()
        if run is None:
            raise ConflictError("尚无已完成的到期评估，不能编制销毁计划")
        evaluation_id = int(run["id"])
        if run["policy_fingerprint"] != self.engine.policy_fingerprint():
            raise ConflictError("策略在最近评估后发生变化，请重新运行每日评估后再编制计划")
        included = [
            item
            for item in self.runs.conclusions(evaluation_id)
            if item["outcome"] == "included" and not item["stale"]
        ]
        if data.get("sample_ids"):
            wanted = set(int(value) for value in data["sample_ids"])
            unknown = wanted - {int(item["sample_id"]) for item in included}
            if unknown:
                raise ValidationError(f"以下样品不在最新有效纳入候选中：{sorted(unknown)}")
            included = [item for item in included if int(item["sample_id"]) in wanted]
        if not included:
            raise ConflictError("没有可纳入成组销毁计划的有效候选")

        unsafe = self._live_blockers([int(item["sample_id"]) for item in included], now)
        if unsafe:
            raise ConflictError("部分候选在评估后出现新的阻塞，拒绝交给销毁计划", context={"blocked": unsafe})

        already_planned = self.batches.planned_sample_ids()
        included = [item for item in included if int(item["sample_id"]) not in already_planned]
        if not included:
            raise ConflictError("候选样品均已列入待执行的销毁计划，不能重复建单")

        batch_code = data.get("batch_code") or f"DEST-{run['evaluation_date']}-{uuid.uuid4().hex[:8]}"
        if self.batches.by_code(batch_code):
            raise ConflictError("销毁计划编号已存在")
        items = [
            {
                "sample_id": int(item["sample_id"]),
                "conclusion_id": int(item["id"]),
                "sample_code": item["sample_code"],
                "quantity": float(item["quantity"]),
                "unit": item["unit"],
            }
            for item in included
        ]
        manifest = hashlib.sha256(
            json.dumps(
                {"evaluation_id": evaluation_id, "items": sorted(items, key=lambda value: value["sample_id"])},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        batch = self.batches.create(
            batch_code=batch_code,
            source_evaluation_id=evaluation_id,
            planned_by=principal.user_id,
            note=data.get("note", ""),
            items=items,
            manifest_digest=manifest,
            now=now,
        )
        self.audit.record(
            principal, "destruction.plan.create", "destruction_batch", str(batch["id"]),
            after=batch, metadata={"evaluation_id": evaluation_id, "manifest_digest": manifest},
        )
        return batch

    def release(self, principal: Principal, batch_id: int) -> dict[str, Any]:
        principal.require("destruction.plan")
        before = self.batches.get(batch_id)
        batch = self.batches.release(batch_id, to_storage(self.clock.now()))
        self.audit.record(
            principal, "destruction.plan.release", "destruction_batch", str(batch_id),
            before={"state": before["state"]}, after={"state": batch["state"]},
        )
        return batch

    def list_batches(self, principal: Principal) -> list[dict[str, Any]]:
        principal.require("retention.report.read")
        return self.batches.list_batches()

    def get(self, principal: Principal, batch_id: int) -> dict[str, Any]:
        principal.require("retention.report.read")
        return self.batches.get(batch_id)

    def _live_blockers(self, sample_ids: list[int], now: str) -> list[dict[str, Any]]:
        """交出计划前再核验一次实时状态，防止评估后新发生的借用、异常、保留或复核被漏掉。"""
        unsafe: list[dict[str, Any]] = []
        for sample_id in sample_ids:
            sample = self.connection.execute(
                """SELECT s.*,b.project_code,b.id AS batch_id FROM samples s
                   JOIN receipt_batches b ON b.id=s.batch_id WHERE s.id=?""",
                (sample_id,),
            ).fetchone()
            if sample is None:
                unsafe.append({"sample_id": sample_id, "reason_codes": ["sample_missing"]})
                continue
            sample_data = dict(sample)
            if sample_data["lifecycle_state"] in {"destroyed", "consumed", "pending_destruction", "quarantined"}:
                unsafe.append({"sample_id": sample_id, "reason_codes": ["state_changed"]})
                continue
            reasons: list[str] = []
            if sample_data["lifecycle_state"] == "loaned" or self.connection.execute(
                "SELECT 1 FROM loans WHERE sample_id=? AND state IN ('active','partially_returned','overdue','disputed') LIMIT 1",
                (sample_id,),
            ).fetchone():
                reasons.append("on_loan")
            if self.connection.execute(
                """SELECT 1 FROM anomaly_cases WHERE state IN ('open','investigating','contained')
                   AND (sample_id=? OR (sample_id IS NULL AND batch_id=?)) LIMIT 1""",
                (sample_id, sample_data["batch_id"]),
            ).fetchone():
                reasons.append("anomaly_open")
            project_code = sample_data["project_code"]
            sample_type = sample_data["sample_type"]
            if self.connection.execute(
                """SELECT 1 FROM legal_holds WHERE state='active' AND starts_at<=?
                   AND (expires_at IS NULL OR expires_at>?)
                   AND ((scope_type='sample' AND sample_id=?)
                        OR (scope_type='project' AND scope_value=?)
                        OR (scope_type='sample_type' AND scope_value=?)) LIMIT 1""",
                (now, now, sample_id, project_code, sample_type),
            ).fetchone():
                reasons.append("legal_hold")
            if self.connection.execute(
                """SELECT 1 FROM publication_reviews WHERE sample_id=? AND review_state='under_review'
                   AND (expected_clear_at IS NULL OR expected_clear_at>?) LIMIT 1""",
                (sample_id, now),
            ).fetchone():
                reasons.append("paper_review")
            if reasons:
                unsafe.append({"sample_id": sample_id, "reason_codes": reasons})
        return unsafe
