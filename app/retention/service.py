from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from calendar import monthrange
from datetime import datetime, timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.database import transaction
from app.samples.operations import DestructionService
from app.samples.repository import ApprovalRepository, SampleRepository
from app.services.audit import AuditService

ACTIVE_LOAN_STATES = ("active", "partially_returned", "overdue", "disputed")
OPEN_ANOMALY_STATES = ("open", "investigating", "contained")
TERMINAL_STATES = ("destroyed", "consumed")


def _digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def match_policy(policies: list[dict[str, Any]], sample_type: str, project_code: str) -> dict[str, Any] | None:
    """按 项目+类型精确 > 类型默认 > 全局默认 的顺序匹配生效策略。"""
    exact = type_default = global_default = None
    for policy in policies:
        if policy["sample_type"] == sample_type and policy["project_code"] == project_code:
            exact = exact or policy
        elif policy["sample_type"] == sample_type and policy["project_code"] is None:
            type_default = type_default or policy
        elif policy["sample_type"] == "*" and policy["project_code"] is None:
            global_default = global_default or policy
    return exact or type_default or global_default


def collect_retention_context(connection: sqlite3.Connection, sample: dict[str, Any]) -> dict[str, Any]:
    """汇总样品的实时阻断因素来源：借用、异常、论文引用、法律保留与延期。"""
    sample_id = sample["id"]
    loans = [
        dict(row)
        for row in connection.execute(
            f"SELECT loan_code,state FROM loans WHERE sample_id=? AND state IN ({','.join('?' * len(ACTIVE_LOAN_STATES))})",
            (sample_id, *ACTIVE_LOAN_STATES),
        ).fetchall()
    ]
    anomalies = [
        dict(row)
        for row in connection.execute(
            "SELECT case_code,severity FROM anomaly_cases WHERE (sample_id=? OR batch_id=?) AND state NOT IN ('resolved','dismissed')",
            (sample_id, sample["batch_id"]),
        ).fetchall()
    ]
    paper_refs = [
        dict(row)
        for row in connection.execute(
            "SELECT reference_code,publication FROM paper_review_refs WHERE sample_id=? AND state='active'",
            (sample_id,),
        ).fetchall()
    ]
    # 法律保留按项目与整条谱系（同一根样品）扩散，确保证据链完整。
    holds = [
        dict(row)
        for row in connection.execute(
            """SELECT DISTINCT h.hold_code,h.reason FROM legal_holds h
               WHERE h.released_at IS NULL AND (
                   h.project_code=? OR h.sample_id IN (SELECT id FROM samples WHERE root_sample_id=?)
               )""",
            (sample["project_code"], sample["root_sample_id"] or sample_id),
        ).fetchall()
    ]
    extensions = [
        dict(row)
        for row in connection.execute(
            "SELECT extension_code,state,extend_until FROM retention_extensions WHERE sample_id=? AND state IN ('pending','approved')",
            (sample_id,),
        ).fetchall()
    ]
    return {"loans": loans, "anomalies": anomalies, "paper_refs": paper_refs, "holds": holds, "extensions": extensions}


def build_blockers(sample: dict[str, Any], context: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if sample["lifecycle_state"] == "quarantined":
        blockers.append({"code": "quarantined", "message": "样品处于隔离状态", "refs": []})
    if sample["lifecycle_state"] == "pending_destruction":
        blockers.append({"code": "already_in_destruction", "message": "样品已在销毁流程中", "refs": []})
    if context["loans"]:
        refs = [item["loan_code"] for item in context["loans"]]
        blockers.append({"code": "loan_active", "message": f"存在未归还借用：{'、'.join(refs)}", "refs": refs})
    if context["anomalies"]:
        refs = [item["case_code"] for item in context["anomalies"]]
        blockers.append({"code": "anomaly_open", "message": f"存在未关闭异常：{'、'.join(refs)}", "refs": refs})
    if context["paper_refs"]:
        refs = [item["reference_code"] for item in context["paper_refs"]]
        blockers.append({"code": "paper_ref_active", "message": f"样品仍被论文复核引用：{'、'.join(refs)}", "refs": refs})
    if context["holds"]:
        refs = [item["hold_code"] for item in context["holds"]]
        blockers.append({"code": "legal_hold", "message": f"样品处于法律保留：{'、'.join(refs)}", "refs": refs})
    pending = [item for item in context["extensions"] if item["state"] == "pending"]
    if pending:
        refs = [item["extension_code"] for item in pending]
        blockers.append({"code": "extension_pending", "message": "保留延期正在双人审批", "refs": refs})
    active = [
        item
        for item in context["extensions"]
        if item["state"] == "approved" and (from_storage(item["extend_until"]) or now) > now
    ]
    if active:
        refs = [item["extension_code"] for item in active]
        blockers.append({"code": "extension_active", "message": "批准的保留延期仍然有效", "refs": refs})
    return blockers


def evaluate_sample(
    connection: sqlite3.Connection,
    sample: dict[str, Any],
    policies: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """对单个样品计算保存期结论：candidate（纳入）、deferred（推迟）或 excluded（排除）。"""
    policy = match_policy(policies, sample["sample_type"], sample["project_code"])
    if policy is None:
        return {
            "decision": "excluded",
            "policy": None,
            "retain_until": None,
            "reasons": [
                {
                    "code": "no_policy",
                    "message": f"样品类型「{sample['sample_type']}」没有匹配的保存策略，无法计算保存期限",
                    "refs": [],
                }
            ],
        }
    context = collect_retention_context(connection, sample)
    received = from_storage(sample["received_at"])
    base_until = add_months(received, int(policy["retention_months"]))
    approved_untils = [
        parsed
        for item in context["extensions"]
        if item["state"] == "approved" and (parsed := from_storage(item["extend_until"])) is not None
    ]
    effective_until = max([base_until, *approved_untils])
    extended = effective_until > base_until
    retain_until = to_storage(effective_until)
    blockers = build_blockers(sample, context, now)
    holds = [item for item in blockers if item["code"] == "legal_hold"]
    if holds:
        return {"decision": "excluded", "policy": policy, "retain_until": retain_until, "reasons": holds}
    if blockers:
        return {"decision": "deferred", "policy": policy, "retain_until": retain_until, "reasons": blockers}
    if now < effective_until:
        reasons = [{"code": "not_yet_due", "message": f"保存期至 {retain_until} 才届满", "refs": []}]
        if extended:
            reasons.append({"code": "extension_active", "message": "批准的保留延期推迟了到期时间", "refs": []})
        return {"decision": "deferred", "policy": policy, "retain_until": retain_until, "reasons": reasons}
    message = (
        f"保存期已于 {retain_until} 届满"
        f"（策略 {policy['policy_code']} v{policy['version']}，{policy['retention_months']} 个月，接收于 {sample['received_at']}）"
    )
    reasons = [{"code": "retention_expired", "message": message, "refs": [policy["policy_code"]]}]
    return {"decision": "candidate", "policy": policy, "retain_until": retain_until, "reasons": reasons}


class RetentionPolicyService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.audit = AuditService(connection, self.clock)

    def create_line(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("retention.manage")
        existing = self.connection.execute(
            "SELECT id FROM retention_policies WHERE policy_code=? AND status='active'",
            (data["policy_code"],),
        ).fetchone()
        if existing:
            raise ConflictError("策略编码已存在，请通过新版本接口变更策略")
        now = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                """INSERT INTO retention_policies(policy_code,version,sample_type,project_code,retention_months,description,status,created_by,created_at)
                   VALUES(?,?,?,?,?,?,'active',?,?)""",
                (
                    data["policy_code"], 1, data["sample_type"], data.get("project_code"),
                    data["retention_months"], data.get("description", ""), principal.user_id, now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("相同样品类型与项目编码的生效策略已存在") from exc
        policy = self.get(policy_id=cursor.lastrowid)
        self.audit.record(principal, "retention.policy.create", "retention_policy", str(policy["id"]), after=policy)
        return policy

    def create_version(self, principal: Principal, policy_code: str, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("retention.manage")
        old = self.connection.execute(
            "SELECT * FROM retention_policies WHERE policy_code=? AND status='active'",
            (policy_code,),
        ).fetchone()
        if old is None:
            raise NotFoundError("策略不存在或已停用")
        now = to_storage(self.clock.now())
        # 先停用旧版本再插入新版本，保证同一编码、同一匹配键只有一个生效版本。
        self.connection.execute("UPDATE retention_policies SET status='superseded' WHERE id=?", (old["id"],))
        cursor = self.connection.execute(
            """INSERT INTO retention_policies(policy_code,version,sample_type,project_code,retention_months,description,status,supersedes_id,created_by,created_at)
               VALUES(?,?,?,?,?,?,'active',?,?,?)""",
            (
                old["policy_code"], old["version"] + 1, old["sample_type"], old["project_code"],
                data["retention_months"], data.get("description", ""), old["id"], principal.user_id, now,
            ),
        )
        # 策略变更：基于旧版本的当前结论标记为过期，历史行保留不改写。
        expired = self.connection.execute(
            "UPDATE retention_evaluations SET status='superseded', superseded_reason='policy_changed' "
            "WHERE policy_id=? AND status='current'",
            (old["id"],),
        ).rowcount
        policy = self.get(policy_id=cursor.lastrowid)
        self.audit.record(
            principal,
            "retention.policy.version",
            "retention_policy",
            str(policy["id"]),
            before=dict(old),
            after=policy,
            metadata={"expired_evaluation_count": expired},
        )
        return {**policy, "expired_evaluation_count": expired}

    def get(self, policy_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM retention_policies WHERE id=?", (policy_id,)).fetchone()
        if row is None:
            raise NotFoundError("保存策略不存在")
        return dict(row)

    def list(self, principal: Principal) -> list[dict[str, Any]]:
        principal.require("retention.read")
        rows = self.connection.execute(
            "SELECT * FROM retention_policies ORDER BY policy_code, version DESC"
        ).fetchall()
        return [dict(row) for row in rows]


class RetentionEvaluationService:
    """每日保存期评估：按样品主键分批推进，检查点落库，可断点恢复且重复运行幂等。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.audit = AuditService(connection, self.clock)

    def run_daily(
        self,
        actor: object,
        run_date: str | None = None,
        *,
        batch_size: int = 200,
        max_batches: int | None = None,
        lease_seconds: int = 300,
        worker: str | None = None,
    ) -> dict[str, Any]:
        now = self.clock.now()
        run_date = run_date or now.date().isoformat()
        try:
            datetime.strptime(run_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValidationError("run_date 必须是 YYYY-MM-DD 格式") from exc
        run_key = f"retention-daily:{run_date}"
        user_id = getattr(actor, "user_id", None)
        worker = worker or (f"user:{user_id}" if user_id else "system")
        policies = self._active_policies(self.connection)
        policy_digest = _digest(
            {
                "policies": [
                    {
                        "id": item["id"],
                        "policy_code": item["policy_code"],
                        "version": item["version"],
                        "sample_type": item["sample_type"],
                        "project_code": item["project_code"],
                        "retention_months": item["retention_months"],
                    }
                    for item in policies
                ]
            }
        )
        run = self._claim(run_key, run_date, policy_digest, worker, lease_seconds, now)
        if run["status"] == "completed":
            return {"run": run, "replayed": True}
        batches = 0
        try:
            while True:
                if max_batches is not None and batches >= max_batches:
                    # 时间片用完：释放租约并保留检查点，等待下次恢复。
                    with transaction(immediate=True) as connection:
                        connection.execute(
                            "UPDATE retention_runs SET locked_by=NULL, locked_at=NULL, updated_at=? WHERE id=? AND locked_by=?",
                            (to_storage(self.clock.now()), run["id"], worker),
                        )
                    break
                with transaction(immediate=True) as connection:
                    rows = connection.execute(
                        """SELECT s.*,b.received_at,b.project_code,b.batch_code
                           FROM samples s JOIN receipt_batches b ON b.id=s.batch_id
                           WHERE s.id>? AND s.lifecycle_state NOT IN ('destroyed','consumed')
                           ORDER BY s.id LIMIT ?""",
                        (run["checkpoint_sample_id"], batch_size),
                    ).fetchall()
                    if not rows:
                        finished = connection.execute(
                            "UPDATE retention_runs SET status='completed', finished_at=?, locked_by=NULL, locked_at=NULL, updated_at=? "
                            "WHERE id=? AND locked_by=?",
                            (to_storage(self.clock.now()), to_storage(self.clock.now()), run["id"], worker),
                        )
                        if finished.rowcount != 1:
                            raise ConflictError("评估任务租约已被其他执行者接管")
                        break
                    counts = {"candidate": 0, "deferred": 0, "excluded": 0, "unchanged": 0}
                    for row in rows:
                        outcome = self._record_evaluation(connection, dict(row), policies, run["id"], now)
                        counts[outcome] += 1
                    last_id = rows[-1]["id"]
                    updated = connection.execute(
                        """UPDATE retention_runs SET checkpoint_sample_id=?, processed_count=processed_count+?,
                           candidate_count=candidate_count+?, deferred_count=deferred_count+?, excluded_count=excluded_count+?,
                           unchanged_count=unchanged_count+?, locked_at=?, updated_at=?
                           WHERE id=? AND locked_by=?""",
                        (
                            last_id, len(rows), counts["candidate"], counts["deferred"], counts["excluded"],
                            counts["unchanged"], to_storage(self.clock.now()), to_storage(self.clock.now()),
                            run["id"], worker,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise ConflictError("评估任务租约已被其他执行者接管")
                    run["checkpoint_sample_id"] = last_id
                    batches += 1
        except Exception as exc:
            if not isinstance(exc, ConflictError):
                with transaction(immediate=True) as connection:
                    connection.execute(
                        "UPDATE retention_runs SET status='failed', error_message=?, locked_by=NULL, locked_at=NULL, updated_at=? "
                        "WHERE id=? AND locked_by=?",
                        (str(exc)[:1000], to_storage(self.clock.now()), run["id"], worker),
                    )
            raise
        run = self.get_run(run["id"])
        self.audit.record(
            actor,
            "retention.run",
            "retention_run",
            str(run["id"]),
            after=run,
            metadata={"run_key": run_key, "batches": batches},
        )
        return {"run": run, "replayed": False}

    def _claim(self, run_key: str, run_date: str, policy_digest: str, worker: str, lease_seconds: int, now: datetime) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            connection.execute(
                """INSERT OR IGNORE INTO retention_runs(run_key,run_date,status,policy_digest,started_at,created_at,updated_at)
                   VALUES(?,?,'running',?,?,?,?)""",
                (run_key, run_date, policy_digest, to_storage(now), to_storage(now), to_storage(now)),
            )
            row = connection.execute("SELECT * FROM retention_runs WHERE run_key=?", (run_key,)).fetchone()
            run = dict(row)
            if run["status"] == "completed":
                return run
            stale_before = to_storage(now - timedelta(seconds=lease_seconds))
            claimed = connection.execute(
                """UPDATE retention_runs SET locked_by=?, locked_at=?, status='running', error_message=NULL, updated_at=?
                   WHERE id=? AND (locked_by IS NULL OR locked_at<? OR locked_by=?)""",
                (worker, to_storage(now), to_storage(now), run["id"], stale_before, worker),
            )
            if claimed.rowcount != 1:
                raise ConflictError("评估任务正在由其他执行者运行，请稍后重试")
            return dict(connection.execute("SELECT * FROM retention_runs WHERE id=?", (run["id"],)).fetchone())

    def _record_evaluation(
        self,
        connection: sqlite3.Connection,
        sample: dict[str, Any],
        policies: list[dict[str, Any]],
        run_id: int,
        now: datetime,
    ) -> str:
        result = evaluate_sample(connection, sample, policies, now)
        policy = result["policy"]
        fingerprint = _digest(
            {
                "policy_id": policy["id"] if policy else None,
                "decision": result["decision"],
                "retain_until": result["retain_until"],
                "reasons": result["reasons"],
            }
        )
        current = connection.execute(
            "SELECT * FROM retention_evaluations WHERE sample_id=? AND status='current'",
            (sample["id"],),
        ).fetchone()
        if current and current["fingerprint"] == fingerprint:
            return "unchanged"
        now_text = to_storage(now)
        if current:
            connection.execute(
                "UPDATE retention_evaluations SET status='superseded', superseded_reason='recomputed' WHERE id=?",
                (current["id"],),
            )
        cursor = connection.execute(
            """INSERT INTO retention_evaluations(sample_id,run_id,policy_id,policy_version,decision,retain_until,reasons_json,fingerprint,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,'current',?)""",
            (
                sample["id"], run_id, policy["id"] if policy else None, policy["version"] if policy else None,
                result["decision"], result["retain_until"], json.dumps(result["reasons"], ensure_ascii=False),
                fingerprint, now_text,
            ),
        )
        if current:
            connection.execute(
                "UPDATE retention_evaluations SET superseded_by=? WHERE id=?",
                (cursor.lastrowid, current["id"]),
            )
        return result["decision"]

    def get_run(self, run_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM retention_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError("评估任务不存在")
        return dict(row)

    def list_runs(self, principal: Principal) -> list[dict[str, Any]]:
        principal.require("retention.read")
        rows = self.connection.execute("SELECT * FROM retention_runs ORDER BY id DESC LIMIT 100").fetchall()
        return [dict(row) for row in rows]

    def list_evaluations(
        self,
        principal: Principal,
        *,
        decision: str | None = None,
        sample_id: int | None = None,
        history: bool = False,
    ) -> list[dict[str, Any]]:
        principal.require("retention.read")
        clauses: list[str] = []
        params: list[Any] = []
        if not history:
            clauses.append("e.status='current'")
        if decision:
            clauses.append("e.decision=?")
            params.append(decision)
        if sample_id is not None:
            clauses.append("e.sample_id=?")
            params.append(sample_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            """SELECT e.*,s.sample_code FROM retention_evaluations e
               JOIN samples s ON s.id=e.sample_id""" + where + " ORDER BY e.id DESC LIMIT 500",
            tuple(params),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["reasons"] = json.loads(item.pop("reasons_json"))
            result.append(item)
        return result

    @staticmethod
    def _active_policies(connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM retention_policies WHERE status='active' ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]


class RetentionExtensionService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.samples = SampleRepository(connection)
        self.approvals = ApprovalRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def request(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("retention.extend")
        sample = self.samples.get(data["sample_id"])
        if sample["lifecycle_state"] in TERMINAL_STATES:
            raise ConflictError("样品已消耗或销毁，无法申请保留延期")
        try:
            extend_until = from_storage(data["extend_until"])
        except ValueError:
            extend_until = None
        if extend_until is None:
            raise ValidationError("延期截止时间格式不正确")
        now = self.clock.now()
        if extend_until <= now:
            raise ValidationError("延期截止时间必须晚于当前时间")
        now_text = to_storage(now)
        approval = self.approvals.create(
            {
                "action_type": "retention_extension",
                "resource_type": "sample",
                "resource_id": sample["id"],
                "payload": {"extend_until": to_storage(extend_until), "reason": data["reason"]},
                "expires_at": to_storage(now + timedelta(days=3)),
            },
            principal.user_id,
            f"APR-{uuid.uuid4().hex[:12]}",
            now_text,
        )
        extension_code = f"EXT-{uuid.uuid4().hex[:12]}"
        cursor = self.connection.execute(
            """INSERT INTO retention_extensions(extension_code,sample_id,request_id,requested_by,extend_until,reason,state,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'pending',?,?)""",
            (extension_code, sample["id"], approval["id"], principal.user_id, to_storage(extend_until), data["reason"], now_text, now_text),
        )
        extension = self.get(cursor.lastrowid)
        self.audit.record(
            principal,
            "retention.extension.request",
            "retention_extension",
            str(extension["id"]),
            after=extension,
            metadata={"request_id": approval["id"]},
        )
        return {**extension, "approval": approval}

    def get(self, extension_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM retention_extensions WHERE id=?", (extension_id,)).fetchone()
        if row is None:
            raise NotFoundError("保留延期不存在")
        return dict(row)

    def list(self, principal: Principal, sample_id: int | None = None) -> list[dict[str, Any]]:
        principal.require("retention.read")
        if sample_id is not None:
            rows = self.connection.execute(
                "SELECT * FROM retention_extensions WHERE sample_id=? ORDER BY id DESC", (sample_id,)
            ).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM retention_extensions ORDER BY id DESC LIMIT 200").fetchall()
        return [dict(row) for row in rows]


def sync_extension_on_decision(
    connection: sqlite3.Connection,
    action_type: str,
    request_id: int,
    state: str,
    now: str,
) -> None:
    """审批结束时同步保留延期状态（由 ApprovalService.decide 调用）。"""
    if action_type != "retention_extension" or state not in ("approved", "rejected"):
        return
    connection.execute(
        "UPDATE retention_extensions SET state=?, updated_at=? WHERE request_id=? AND state='pending'",
        (state, now, request_id),
    )


class LegalHoldService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.samples = SampleRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def place(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("retention.manage")
        if data.get("sample_id"):
            self.samples.get(data["sample_id"])
        hold_code = data.get("hold_code") or f"LH-{uuid.uuid4().hex[:12]}"
        now = to_storage(self.clock.now())
        cursor = self.connection.execute(
            """INSERT INTO legal_holds(hold_code,sample_id,project_code,reason,placed_by,placed_at)
               VALUES(?,?,?,?,?,?)""",
            (hold_code, data.get("sample_id"), data.get("project_code"), data["reason"], principal.user_id, now),
        )
        hold = self.get(cursor.lastrowid)
        self.audit.record(principal, "retention.hold.place", "legal_hold", str(hold["id"]), after=hold)
        return hold

    def release(self, principal: Principal, hold_id: int, note: str) -> dict[str, Any]:
        principal.require("retention.manage")
        before = self.get(hold_id)
        if before["released_at"]:
            raise ConflictError("法律保留已经解除")
        now = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE legal_holds SET released_by=?, released_at=?, release_note=? WHERE id=?",
            (principal.user_id, now, note, hold_id),
        )
        hold = self.get(hold_id)
        self.audit.record(principal, "retention.hold.release", "legal_hold", str(hold_id), before=before, after=hold)
        return hold

    def get(self, hold_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM legal_holds WHERE id=?", (hold_id,)).fetchone()
        if row is None:
            raise NotFoundError("法律保留不存在")
        return dict(row)

    def list(self, principal: Principal, active_only: bool = True) -> list[dict[str, Any]]:
        principal.require("retention.read")
        sql = "SELECT * FROM legal_holds"
        if active_only:
            sql += " WHERE released_at IS NULL"
        sql += " ORDER BY id DESC LIMIT 200"
        return [dict(row) for row in self.connection.execute(sql).fetchall()]


class PaperReviewService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.samples = SampleRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def add(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("retention.manage")
        self.samples.get(data["sample_id"])
        reference_code = data.get("reference_code") or f"PR-{uuid.uuid4().hex[:12]}"
        now = to_storage(self.clock.now())
        cursor = self.connection.execute(
            """INSERT INTO paper_review_refs(reference_code,sample_id,publication,note,state,created_by,created_at)
               VALUES(?,?,?,?,'active',?,?)""",
            (reference_code, data["sample_id"], data["publication"], data.get("note", ""), principal.user_id, now),
        )
        ref = self.get(cursor.lastrowid)
        self.audit.record(principal, "retention.paper_ref.add", "paper_review_ref", str(ref["id"]), after=ref)
        return ref

    def release(self, principal: Principal, ref_id: int) -> dict[str, Any]:
        principal.require("retention.manage")
        before = self.get(ref_id)
        if before["state"] == "released":
            raise ConflictError("论文复核引用已经解除")
        now = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE paper_review_refs SET state='released', released_by=?, released_at=? WHERE id=?",
            (principal.user_id, now, ref_id),
        )
        ref = self.get(ref_id)
        self.audit.record(principal, "retention.paper_ref.release", "paper_review_ref", str(ref_id), before=before, after=ref)
        return ref

    def get(self, ref_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM paper_review_refs WHERE id=?", (ref_id,)).fetchone()
        if row is None:
            raise NotFoundError("论文复核引用不存在")
        return dict(row)

    def list(self, principal: Principal, active_only: bool = True) -> list[dict[str, Any]]:
        principal.require("retention.read")
        sql = "SELECT * FROM paper_review_refs"
        if active_only:
            sql += " WHERE state='active'"
        sql += " ORDER BY id DESC LIMIT 200"
        return [dict(row) for row in self.connection.execute(sql).fetchall()]


class RetentionReportService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.audit = AuditService(connection, self.clock)

    def generate(self, principal: Principal, run_id: int | None = None) -> dict[str, Any]:
        principal.require("retention.manage")
        run = None
        if run_id is not None:
            row = self.connection.execute("SELECT * FROM retention_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise NotFoundError("评估任务不存在")
            run = dict(row)
            if run["status"] != "completed":
                raise ConflictError("评估任务尚未完成，不能生成候选报告")
        else:
            row = self.connection.execute(
                "SELECT * FROM retention_runs WHERE status='completed' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            run = dict(row) if row else None
        evaluations = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM retention_evaluations WHERE status='current' ORDER BY sample_id"
            ).fetchall()
        ]
        now = to_storage(self.clock.now())
        report_code = f"RPT-{uuid.uuid4().hex[:12]}"
        cursor = self.connection.execute(
            """INSERT INTO retention_reports(report_code,run_id,generated_by,candidate_count,deferred_count,excluded_count,created_at)
               VALUES(?,?,?,0,0,0,?)""",
            (report_code, run["id"] if run else None, principal.user_id, now),
        )
        report_id = cursor.lastrowid
        counts = {"candidate": 0, "deferred": 0, "excluded": 0}
        for evaluation in evaluations:
            counts[evaluation["decision"]] += 1
            self.connection.execute(
                """INSERT INTO retention_report_items(report_id,evaluation_id,sample_id,decision,retain_until,reasons_json,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    report_id, evaluation["id"], evaluation["sample_id"], evaluation["decision"],
                    evaluation["retain_until"], evaluation["reasons_json"], now,
                ),
            )
        self.connection.execute(
            "UPDATE retention_reports SET candidate_count=?, deferred_count=?, excluded_count=? WHERE id=?",
            (counts["candidate"], counts["deferred"], counts["excluded"], report_id),
        )
        report = self._detail(report_id)
        self.audit.record(
            principal,
            "retention.report.generate",
            "retention_report",
            str(report_id),
            after=report["report"],
            metadata={"run_id": run["id"] if run else None},
        )
        return report

    def detail(self, principal: Principal, report_id: int) -> dict[str, Any]:
        principal.require("retention.read")
        return self._detail(report_id)

    def _detail(self, report_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM retention_reports WHERE id=?", (report_id,)).fetchone()
        if row is None:
            raise NotFoundError("候选报告不存在")
        report = dict(row)
        run = None
        if report["run_id"]:
            run_row = self.connection.execute("SELECT * FROM retention_runs WHERE id=?", (report["run_id"],)).fetchone()
            run = dict(run_row) if run_row else None
        items: dict[str, list[dict[str, Any]]] = {"candidate": [], "deferred": [], "excluded": []}
        rows = self.connection.execute(
            """SELECT i.*,s.sample_code,s.sample_type,s.lifecycle_state
               FROM retention_report_items i JOIN samples s ON s.id=i.sample_id
               WHERE i.report_id=? ORDER BY i.decision, s.sample_code""",
            (report_id,),
        ).fetchall()
        for item_row in rows:
            item = dict(item_row)
            item["reasons"] = json.loads(item.pop("reasons_json"))
            items[item["decision"]].append(item)
        return {"report": report, "run": run, "items": items}

    def list(self, principal: Principal) -> list[dict[str, Any]]:
        principal.require("retention.read")
        rows = self.connection.execute("SELECT * FROM retention_reports ORDER BY id DESC LIMIT 100").fetchall()
        return [dict(row) for row in rows]


class DestructionPlanService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.samples = SampleRepository(connection)
        self.approvals = ApprovalRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def create_from_report(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("retention.manage")
        report_row = self.connection.execute("SELECT * FROM retention_reports WHERE id=?", (data["report_id"],)).fetchone()
        if report_row is None:
            raise NotFoundError("候选报告不存在")
        clauses = ["i.report_id=?", "i.decision='candidate'"]
        params: list[Any] = [data["report_id"]]
        if data.get("sample_ids"):
            placeholders = ",".join("?" for _ in data["sample_ids"])
            clauses.append(f"i.sample_id IN ({placeholders})")
            params.extend(data["sample_ids"])
        rows = self.connection.execute(
            """SELECT i.*,s.sample_code FROM retention_report_items i JOIN samples s ON s.id=i.sample_id
               WHERE """ + " AND ".join(clauses) + " ORDER BY i.sample_id",
            tuple(params),
        ).fetchall()
        valid: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            problems = self._safety_problems(item)
            if problems:
                skipped.append({"sample_id": item["sample_id"], "sample_code": item["sample_code"], "reasons": problems})
            else:
                valid.append(item)
        if not valid:
            raise ConflictError("没有可安全纳入销毁计划的候选样品", context={"skipped": skipped})
        now = to_storage(self.clock.now())
        plan_code = f"DPL-{uuid.uuid4().hex[:12]}"
        cursor = self.connection.execute(
            "INSERT INTO destruction_plans(plan_code,report_id,state,created_by,created_at,updated_at) VALUES(?,?,'draft',?,?,?)",
            (plan_code, data["report_id"], principal.user_id, now, now),
        )
        plan_id = cursor.lastrowid
        for item in valid:
            self.connection.execute(
                """INSERT INTO destruction_plan_items(plan_id,sample_id,evaluation_id,state,created_at,updated_at)
                   VALUES(?,?,?,'pending',?,?)""",
                (plan_id, item["sample_id"], item["evaluation_id"], now, now),
            )
        plan = self._detail(plan_id)
        self.audit.record(
            principal,
            "retention.plan.create",
            "destruction_plan",
            str(plan_id),
            after=plan["plan"],
            metadata={"item_count": len(valid), "skipped_count": len(skipped)},
        )
        return {**plan, "skipped": skipped}

    def submit(self, principal: Principal, plan_id: int) -> dict[str, Any]:
        principal.require("samples.destroy")
        plan = self._get_plan(plan_id)
        if plan["state"] not in ("draft", "submitted"):
            raise ConflictError("当前状态的销毁计划不能提交审批")
        items = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM destruction_plan_items WHERE plan_id=? AND state='pending' ORDER BY sample_id",
                (plan_id,),
            ).fetchall()
        ]
        if not items:
            raise ConflictError("销毁计划没有待提交的明细")
        now = self.clock.now()
        now_text = to_storage(now)
        requested: list[int] = []
        skipped: list[dict[str, Any]] = []
        for item in items:
            problems = self._safety_problems(item, exclude_plan_item_id=item["id"])
            if problems:
                sample = self.samples.get(item["sample_id"])
                skipped.append({"sample_id": item["sample_id"], "sample_code": sample["sample_code"], "reasons": problems})
                continue
            sample = self.samples.get(item["sample_id"])
            approval = self.approvals.create(
                {
                    "action_type": "destruction",
                    "resource_type": "sample",
                    "resource_id": item["sample_id"],
                    "payload": {
                        "plan_id": plan_id,
                        "plan_item_id": item["id"],
                        "quantity": sample["quantity"],
                        "evaluation_id": item["evaluation_id"],
                    },
                    "expires_at": to_storage(now + timedelta(days=7)),
                },
                principal.user_id,
                f"APR-{uuid.uuid4().hex[:12]}",
                now_text,
            )
            self.connection.execute(
                "UPDATE destruction_plan_items SET state='approval_requested', approval_request_id=?, updated_at=? WHERE id=?",
                (approval["id"], now_text, item["id"]),
            )
            requested.append(item["id"])
        if not requested:
            raise ConflictError("所有明细当前都不满足销毁安全条件", context={"skipped": skipped})
        self.connection.execute(
            "UPDATE destruction_plans SET state='submitted', updated_at=? WHERE id=?",
            (now_text, plan_id),
        )
        detail = self._detail(plan_id)
        self.audit.record(
            principal,
            "retention.plan.submit",
            "destruction_plan",
            str(plan_id),
            before=plan,
            after=detail["plan"],
            metadata={"requested_item_ids": requested, "skipped_count": len(skipped)},
        )
        return {**detail, "requested_item_ids": requested, "skipped": skipped}

    def execute_item(self, principal: Principal, plan_id: int, item_id: int, data: dict[str, Any]) -> dict[str, Any]:
        plan = self._get_plan(plan_id)
        item_row = self.connection.execute(
            "SELECT * FROM destruction_plan_items WHERE id=? AND plan_id=?", (item_id, plan_id)
        ).fetchone()
        if item_row is None:
            raise NotFoundError("销毁计划明细不存在")
        item = dict(item_row)
        if item["state"] == "executed":
            return {**self._detail(plan_id), "replayed": True}
        if item["state"] != "approval_requested":
            raise ConflictError("明细尚未提交双人审批")
        approval = self.approvals.get(item["approval_request_id"])
        if approval["state"] != "approved":
            raise ConflictError("销毁审批尚未通过双人复核")
        problems = self._safety_problems(item, exclude_plan_item_id=item["id"])
        if problems:
            raise ConflictError("样品当前不满足销毁安全条件，已阻止执行", context={"problems": problems})
        result = DestructionService(self.connection, self.clock).execute(principal, item["approval_request_id"], data)
        now_text = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE destruction_plan_items SET state='executed', destruction_record_id=?, updated_at=? WHERE id=?",
            (result["record"]["id"], now_text, item_id),
        )
        remaining = self.connection.execute(
            "SELECT COUNT(*) FROM destruction_plan_items WHERE plan_id=? AND state<>'executed'",
            (plan_id,),
        ).fetchone()[0]
        if remaining == 0:
            self.connection.execute(
                "UPDATE destruction_plans SET state='completed', updated_at=? WHERE id=?",
                (now_text, plan_id),
            )
        detail = self._detail(plan_id)
        self.audit.record(
            principal,
            "retention.plan.execute_item",
            "destruction_plan",
            str(plan_id),
            metadata={"item_id": item_id, "destruction_record_id": result["record"]["id"]},
        )
        return {**detail, "destruction": result, "replayed": False}

    def detail(self, principal: Principal, plan_id: int) -> dict[str, Any]:
        principal.require("retention.read")
        return self._detail(plan_id)

    def _detail(self, plan_id: int) -> dict[str, Any]:
        plan = self._get_plan(plan_id)
        items = [
            dict(row)
            for row in self.connection.execute(
                """SELECT i.*,s.sample_code,s.sample_type,s.lifecycle_state
                   FROM destruction_plan_items i JOIN samples s ON s.id=i.sample_id
                   WHERE i.plan_id=? ORDER BY i.sample_id""",
                (plan_id,),
            ).fetchall()
        ]
        return {"plan": plan, "items": items}

    def list(self, principal: Principal) -> list[dict[str, Any]]:
        principal.require("retention.read")
        rows = self.connection.execute("SELECT * FROM destruction_plans ORDER BY id DESC LIMIT 100").fetchall()
        return [dict(row) for row in rows]

    def _get_plan(self, plan_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM destruction_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("销毁计划不存在")
        return dict(row)

    def _safety_problems(self, item: dict[str, Any], exclude_plan_item_id: int | None = None) -> list[dict[str, Any]]:
        """移交销毁前的实时安全复核：结论必须仍然有效，且不存在任何阻断因素。"""
        problems: list[dict[str, Any]] = []
        evaluation = self.connection.execute(
            "SELECT status FROM retention_evaluations WHERE id=?", (item["evaluation_id"],)
        ).fetchone()
        if evaluation is None or evaluation["status"] != "current":
            problems.append({"code": "evaluation_stale", "message": "评估结论已过期，需要重新运行每日评估", "refs": []})
        row = self.connection.execute(
            """SELECT s.*,b.received_at,b.project_code,b.batch_code
               FROM samples s JOIN receipt_batches b ON b.id=s.batch_id WHERE s.id=?""",
            (item["sample_id"],),
        ).fetchone()
        if row is None:
            return [{"code": "sample_missing", "message": "样品不存在", "refs": []}]
        sample = dict(row)
        if sample["lifecycle_state"] in TERMINAL_STATES:
            problems.append({"code": "lifecycle_terminal", "message": "样品已消耗或销毁", "refs": []})
            return problems
        context = collect_retention_context(self.connection, sample)
        problems.extend(build_blockers(sample, context, self.clock.now()))
        planned = self.connection.execute(
            """SELECT p.plan_code FROM destruction_plan_items i
               JOIN destruction_plans p ON p.id=i.plan_id
               WHERE i.sample_id=? AND i.id<>IFNULL(?,-1) AND i.state IN ('pending','approval_requested')
                 AND p.state IN ('draft','submitted')""",
            (item["sample_id"], exclude_plan_item_id),
        ).fetchone()
        if planned:
            problems.append({"code": "already_planned", "message": f"样品已在其他销毁计划 {planned['plan_code']} 中", "refs": [planned["plan_code"]]})
        return problems
