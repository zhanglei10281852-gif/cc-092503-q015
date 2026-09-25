"""每日到期评估调度：通过后台任务去重建单，worker 领取后执行，可检查点恢复。"""
from __future__ import annotations

import json
import sqlite3

from app.core.clock import Clock, SystemClock
from app.retention.engine import RetentionEvaluationService
from app.services.jobs import JobService

JOB_TYPE = "retention.daily-evaluation"


class RetentionScheduler:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.jobs = JobService(connection, self.clock)

    def enqueue_daily(self, *, evaluation_date: str | None = None) -> dict:
        """每天同一日期只有一个评估任务；重复入队返回原任务，不会重复建单。"""
        date_text = evaluation_date or self.clock.now().date().isoformat()
        return self.jobs.enqueue(
            JOB_TYPE,
            f"retention-evaluation:{date_text}",
            {"evaluation_date": date_text},
        )


def run_evaluation_job(connection: sqlite3.Connection, job: dict, *, clock: Clock | None = None, worker: str = "retention-worker") -> dict:
    """worker 执行入口：成功或可恢复失败均通过任务状态反映，重跑同一天只恢复不重建。

    评估引擎自行管理检查点事务，此处只负责任务领取状态的落盘（连接为自动提交）。
    """
    clock = clock or SystemClock()
    jobs = JobService(connection, clock)
    payload = json.loads(job["payload_json"])
    try:
        result = RetentionEvaluationService(connection, clock).run_for_date(
            payload["evaluation_date"], started_by=worker
        )
    except Exception as exc:  # noqa: BLE001 - worker 必须捕获并记录，避免任务僵死
        jobs.fail(int(job["id"]), worker, str(exc), retry_seconds=300)
        raise
    jobs.complete(int(job["id"]), worker, {
        "evaluation_id": result["evaluation"]["id"],
        "status": result["evaluation"]["status"],
        "replayed": result.get("replayed", False),
        "resumed": result.get("resumed", False),
    })
    return result


def process_due_jobs(connection: sqlite3.Connection, *, clock: Clock | None = None, worker: str = "retention-worker", limit: int = 5) -> list[dict]:
    """CLI worker 循环：领取到期任务并执行（不使用外层事务，评估内部自行提交检查点）。"""
    clock = clock or SystemClock()
    jobs = JobService(connection, clock)
    processed: list[dict] = []
    for _ in range(max(1, limit)):
        job = jobs.claim(worker)
        if job is None:
            break
        if job["job_type"] != JOB_TYPE:
            jobs.fail(int(job["id"]), worker, "未知任务类型", retry_seconds=60)
            processed.append({"job_id": job["id"], "skipped": True})
            continue
        result = run_evaluation_job(connection, job, clock=clock, worker=worker)
        processed.append({"job_id": job["id"], **result["evaluation"]})
    return processed
