"""到期评估引擎：解析版本化策略，逐样品给出纳入/排除/推迟结论，支持检查点恢复。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.database import transaction
from app.retention.repository import (
    DestructionBatchRepository,
    EvaluationRepository,
    LegalHoldRepository,
    PublicationReviewRepository,
    RetentionExtensionRepository,
    RetentionPolicyRepository,
)

# 终态：不再参与保存期限计算
TERMINAL_STATES = {"destroyed", "consumed"}
# 借用中（含部分归还、逾期、争议）的借用记录状态
ACTIVE_LOAN_STATES = ("active", "partially_returned", "overdue", "disputed")
# 未结案的异常状态
OPEN_ANOMALY_STATES = ("open", "investigating", "contained")

def _batch_size() -> int:
    try:
        return max(1, int(os.getenv("SAMPLE_RETENTION_BATCH_SIZE", "200")))
    except ValueError:
        return 200


class RetentionEvaluationService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.policies = RetentionPolicyRepository(connection)
        self.holds = LegalHoldRepository(connection)
        self.reviews = PublicationReviewRepository(connection)
        self.extensions = RetentionExtensionRepository(connection)
        self.runs = EvaluationRepository(connection)
        self.batches = DestructionBatchRepository(connection)

    # ------------------------------------------------------------------ 策略解析
    def resolve_policy(self, *, project_code: str, sample_type: str) -> dict[str, Any]:
        """项目约定优先，其次样品类型，最后机构默认策略。"""
        return (
            self.policies.active_for("project", project_code)
            or self.policies.active_for("sample_type", sample_type)
            or self.policies.active_for("global", "")
        )

    def policy_fingerprint(self) -> str:
        active = self.policies.active_policies()
        canonical = json.dumps(
            [
                {
                    "id": item["id"],
                    "scope_type": item["scope_type"],
                    "scope_value": item["scope_value"],
                    "retain_days": item["retain_days"],
                    "legal_hold_days": item["legal_hold_days"],
                }
                for item in active
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    # ------------------------------------------------------------------ 主流程
    def run_for_date(self, evaluation_date: str, *, started_by: str = "scheduler") -> dict[str, Any]:
        """执行（或从检查点恢复）指定日期的评估。

        评估自行按批次提交事务，使检查点在进程中断后仍可恢复；同一天重复运行只恢复或
        返回已完成结果，不会产生第二份评估或销毁计划。
        """
        now = self.clock.now()
        now_text = to_storage(now)
        with transaction(immediate=True):
            run = self.runs.get_by_date(evaluation_date)
            resumed = False
            if run is None:
                total = self.connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
                run = self.runs.create(evaluation_date, self.policy_fingerprint(), int(total), started_by, now_text)
            elif run["status"] == "completed":
                return {"evaluation": run, "resumed": False, "replayed": True, "checkpoint_resumed": False}
            elif run["status"] == "failed":
                self.connection.execute(
                    "UPDATE retention_evaluations SET status='running',error_message=NULL,updated_at=? WHERE id=?",
                    (now_text, run["id"]),
                )
                run = self.runs.get(run["id"])
                resumed = True
            else:
                resumed = True

        try:
            self._process(run["id"], now_dt=now)
        except Exception:
            failed_at = to_storage(self.clock.now())
            with transaction(immediate=True):
                self.runs.mark_failed(run["id"], "评估中断，可从检查点恢复", failed_at)
            raise
        with transaction(immediate=True):
            final_run = self.runs.get(run["id"])
        return {"evaluation": final_run, "resumed": resumed, "replayed": False, "checkpoint_resumed": resumed}

    def _process(self, evaluation_id: int, *, now_dt) -> None:
        """分批扫描样品并逐批提交，检查点（last_id）随批次落盘。"""
        while True:
            with transaction(immediate=True):
                checkpoint = self.runs.checkpoint(evaluation_id)
                last_id = int(checkpoint.get("last_id", 0))
                rows = self.connection.execute(
                    """SELECT s.*,b.project_code,b.received_at
                       FROM samples s JOIN receipt_batches b ON b.id=s.batch_id
                       WHERE s.id>? ORDER BY s.id LIMIT ?""",
                    (last_id, _batch_size()),
                ).fetchall()
                if not rows:
                    self._finish(evaluation_id)
                    return

                done_ids = self.runs.conclusion_processed_sample_ids(evaluation_id)
                now_text = to_storage(self.clock.now())
                planned_ids = self.batches.planned_sample_ids()
                batch_max_id = last_id
                for row in rows:
                    sample = dict(row)
                    batch_max_id = sample["id"]
                    if sample["id"] in done_ids:
                        continue
                    conclusion = self._classify(sample, planned_ids, now_dt, now_text)
                    self.runs.upsert_conclusion(evaluation_id, conclusion)
                counts = {
                    outcome: int(count)
                    for outcome, count in self.connection.execute(
                        "SELECT outcome,COUNT(*) FROM retention_conclusions WHERE evaluation_id=? GROUP BY outcome",
                        (evaluation_id,),
                    ).fetchall()
                }
                counters = {
                    "processed": sum(counts.values()),
                    "included": counts.get("included", 0),
                    "excluded": counts.get("excluded", 0),
                    "deferred": counts.get("deferred", 0),
                }
                self.runs.save_checkpoint(evaluation_id, {"last_id": batch_max_id}, counters, to_storage(self.clock.now()))

    def _finish(self, evaluation_id: int) -> None:
        checkpoint = self.runs.checkpoint(evaluation_id)
        last_id = int(checkpoint.get("last_id", 0))
        counts = {
            outcome: int(count)
            for outcome, count in self.connection.execute(
                "SELECT outcome,COUNT(*) FROM retention_conclusions WHERE evaluation_id=? GROUP BY outcome",
                (evaluation_id,),
            ).fetchall()
        }
        counters = {
            "processed": sum(counts.values()),
            "included": counts.get("included", 0),
            "excluded": counts.get("excluded", 0),
            "deferred": counts.get("deferred", 0),
        }
        self.runs.complete(
            evaluation_id, {"last_id": last_id, "completed": True}, counters, to_storage(self.clock.now())
        )

    # ------------------------------------------------------------------ 单样品判定
    def _classify(self, sample: dict[str, Any], planned_ids: set[int], now_dt, now_text: str) -> dict[str, Any]:
        sample_id = int(sample["id"])
        base: dict[str, Any] = {
            "sample_id": sample_id,
            "created_at": now_text,
            "applied_extension_ids": [],
            "blocking_hold_ids": [],
            "blocking_loan_ids": [],
            "blocking_anomaly_ids": [],
            "blocking_review_ids": [],
        }

        # 1) 终态与已进入销毁流程：结构性排除
        if sample["lifecycle_state"] in TERMINAL_STATES:
            return {
                **base,
                "outcome": "excluded",
                "reason_code": "terminal_state",
                "reason_detail": f"样品处于终态 {sample['lifecycle_state']}，不再进入销毁候选",
            }
        if sample["lifecycle_state"] == "pending_destruction" or sample_id in planned_ids:
            return {
                **base,
                "outcome": "excluded",
                "reason_code": "already_scheduled",
                "reason_detail": "样品已在销毁流程或已列入成组销毁计划",
            }

        # 2) 临时阻塞：未结案异常（含批次级异常）、借用、论文复核、法律保留、隔离、待审批延期
        blockers: list[tuple[str, str | None, str, str]] = []

        anomaly_rows = self.connection.execute(
            f"""SELECT id FROM anomaly_cases
               WHERE state IN ({','.join('?' for _ in OPEN_ANOMALY_STATES)})
                 AND (sample_id=? OR (sample_id IS NULL AND batch_id=?))
               ORDER BY id""",
            (*OPEN_ANOMALY_STATES, sample_id, sample["batch_id"]),
        ).fetchall()
        if anomaly_rows:
            ids = [int(row[0]) for row in anomaly_rows]
            base["blocking_anomaly_ids"] = ids
            blockers.append(("anomaly_open", None, "存在未结案异常", "anomaly"))

        loan_rows = self.connection.execute(
            f"""SELECT id,due_at FROM loans
                WHERE sample_id=? AND state IN ({','.join('?' for _ in ACTIVE_LOAN_STATES)})
                ORDER BY due_at DESC""",
            (sample_id, *ACTIVE_LOAN_STATES),
        ).fetchall()
        if loan_rows:
            ids = [int(row[0]) for row in loan_rows]
            base["blocking_loan_ids"] = ids
            blockers.append(("on_loan", loan_rows[0]["due_at"], "样品正在借用中，归还后方可销毁", "loan"))

        review_rows = self.reviews.active_for_sample(sample_id, now=now_text)
        if review_rows:
            ids = [int(row["id"]) for row in review_rows]
            base["blocking_review_ids"] = ids
            clears = [row["expected_clear_at"] for row in review_rows if row["expected_clear_at"]]
            blockers.append(("paper_review", max(clears) if clears else None, "样品仍被论文复核引用", "review"))

        hold_rows = self.holds.active_for_sample(
            sample_id,
            project_code=sample["project_code"],
            sample_type=sample["sample_type"],
            now=now_text,
        )
        if hold_rows:
            ids = [int(row["id"]) for row in hold_rows]
            base["blocking_hold_ids"] = ids
            expiries = [row["expires_at"] for row in hold_rows if row["expires_at"]]
            indefinite = any(row["expires_at"] is None for row in hold_rows)
            blockers.append((
                "legal_hold",
                None if indefinite else max(expiries),
                "处于法律保留期，禁止销毁" if indefinite else "处于限期法律保留期",
                "hold",
            ))

        # 谱系依赖：后代样品仍被论文复核引用或处于法律保留时，源样品须一并保留以供复现
        lineage_reasons = self._lineage_blockers(sample_id, now_text, base)
        blockers.extend(lineage_reasons)

        if sample["lifecycle_state"] == "quarantined":
            blockers.append(("quarantined", None, "样品处于隔离状态，需先解除异常", "anomaly"))

        pending_extensions = self.connection.execute(
            "SELECT id,expires_at FROM retention_extensions WHERE sample_id=? AND state='pending' ORDER BY id",
            (sample_id,),
        ).fetchall()
        active_pending = [row for row in pending_extensions if row["expires_at"] > now_text]
        if active_pending:
            blockers.append((
                "extension_pending",
                active_pending[-1]["expires_at"],
                "有保留延期申请尚未完成双人审批",
                "review",
            ))

        # 3) 到期时钟：接收时间 + 类型/项目策略 + 法定保留 + 已批准延期
        policy = self.resolve_policy(project_code=sample["project_code"], sample_type=sample["sample_type"])
        if policy is None:
            return {
                **base,
                "outcome": "deferred",
                "reason_code": "policy_missing",
                "reason_detail": "没有匹配的生效保存策略（含机构默认策略），需库管员补齐策略后重评",
                "deferred_until": None,
            }
        received_at = from_storage(sample["received_at"])
        natural_until = received_at + timedelta(days=int(policy["retain_days"]))
        statutory_until = received_at + timedelta(days=int(policy["legal_hold_days"]))
        eligible_at = max(natural_until, statutory_until)
        approved = self.extensions.approved_for_sample(sample_id)
        if approved:
            extra = sum(int(item["extra_days"]) for item in approved)
            eligible_at = eligible_at + timedelta(days=extra)
            base["applied_extension_ids"] = [int(item["id"]) for item in approved]
        base["policy_version_id"] = policy["id"]
        base["base_retention_date"] = to_storage(natural_until)
        base["eligible_at"] = to_storage(eligible_at)

        if eligible_at > now_dt:
            return {
                **base,
                "outcome": "excluded",
                "reason_code": "not_due",
                "reason_detail": (
                    f"按 {self._policy_label(policy)} 保存期限尚未到期，"
                    f"最早可销毁时间 {base['eligible_at']}"
                ),
                "deferred_until": None,
            }

        # 已到期：有临时阻塞则推迟，并给出最早解除时间
        if blockers:
            primary = blockers[0]
            deferred_untils = [item[1] for item in blockers if item[1]]
            reasons = "；".join(dict.fromkeys(item[2] for item in blockers))
            return {
                **base,
                "outcome": "deferred",
                "reason_code": primary[0],
                "reason_detail": f"保存期限已届满，但{reasons}",
                "deferred_until": max(deferred_untils) if deferred_untils else None,
            }

        return {
            **base,
            "outcome": "included",
            "reason_code": "due",
            "reason_detail": (
                f"按 {self._policy_label(policy)} 保存期限已于 {base['eligible_at']} 届满，"
                "无借用、异常、论文复核或法律保留阻塞"
            ),
            "deferred_until": None,
        }

    def _lineage_blockers(self, sample_id: int, now_text: str, base: dict[str, Any]) -> list[tuple[str, str | None, str, str]]:
        """直系后代（递归谱系）仍被复核引用或被样品级法律保留时，祖先样品推迟销毁，保证可复现。"""
        rows = self.connection.execute(
            """WITH RECURSIVE descendants(ancestor_id, descendant_id, depth) AS (
                   SELECT id, id, 0 FROM samples WHERE id=?
                   UNION ALL
                   SELECT d.ancestor_id, s.id, d.depth+1
                   FROM descendants d JOIN samples s ON s.parent_sample_id=d.descendant_id
                   WHERE d.depth < 50
               )
               SELECT descendant_id FROM descendants WHERE depth>0
                 AND descendant_id NOT IN (
                   SELECT id FROM samples WHERE lifecycle_state IN ('destroyed','consumed')
               )""",
            (sample_id,),
        ).fetchall()
        result: list[tuple[str, str | None, str, str]] = []
        for row in rows:
            descendant_id = int(row[0])
            reviews = self.reviews.active_for_sample(descendant_id, now=now_text)
            holds = self.holds.active_for_sample(
                descendant_id, project_code="", sample_type="", now=now_text
            )
            holds = [item for item in holds if item["scope_type"] == "sample" and item["sample_id"] == descendant_id]
            if reviews:
                base["blocking_review_ids"].extend(int(item["id"]) for item in reviews)
            if holds:
                base["blocking_hold_ids"].extend(int(item["id"]) for item in holds)
            if reviews or holds:
                result.append((
                    "lineage_dependent",
                    None,
                    f"谱系后代样品 {descendant_id} 仍被论文复核引用或处于法律保留",
                    "lineage",
                ))
        return result

    @staticmethod
    def _policy_label(policy: dict[str, Any]) -> str:
        if policy["scope_type"] == "global":
            return "机构默认策略"
        if policy["scope_type"] == "project":
            return f"项目 {policy['scope_value']} 约定"
        return f"样品类型 {policy['scope_value']} 策略"

    # ------------------------------------------------------------------ 报告
    def report(self, evaluation_id: int) -> dict[str, Any]:
        run = self.runs.get(evaluation_id)
        conclusions = self.runs.conclusions(evaluation_id)
        grouped = {"included": [], "excluded": [], "deferred": []}
        for item in conclusions:
            grouped[item["outcome"]].append(item)
        return {
            "evaluation": run,
            "summary": {
                "total": len(conclusions),
                "included": len(grouped["included"]),
                "excluded": len(grouped["excluded"]),
                "deferred": len(grouped["deferred"]),
                "stale_conclusions": sum(1 for item in conclusions if item["stale"]),
            },
            "included": grouped["included"],
            "excluded": grouped["excluded"],
            "deferred": grouped["deferred"],
        }

    def latest_report(self) -> dict[str, Any]:
        run = self.runs.latest_completed()
        if run is None:
            return {"evaluation": None, "summary": None, "included": [], "excluded": [], "deferred": []}
        return self.report(int(run["id"]))
