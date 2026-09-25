from __future__ import annotations

import argparse
import json
import sqlite3

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db


def command_init() -> None:
    init_db()
    print(json.dumps({"database": str(database_path()), "initialized": True}, ensure_ascii=False))


def command_check() -> None:
    init_db()
    connection = get_connection()
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    print(json.dumps({"integrity": integrity, "foreign_keys": foreign_keys, "journal_mode": journal_mode}, ensure_ascii=False))
    if integrity != "ok" or foreign_keys != 1:
        raise SystemExit(1)


def command_smoke() -> None:
    from app.main import app

    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
        print(json.dumps({"root": root.status_code, "health": health.status_code, "service": root.json().get("service")}, ensure_ascii=False))
        if root.status_code != 200 or health.status_code != 200:
            raise SystemExit(1)


def command_retention_enqueue(date: str | None) -> None:
    from app.database import transaction
    from app.retention.scheduler import RetentionScheduler

    with transaction(immediate=True) as connection:
        job = RetentionScheduler(connection).enqueue_daily(evaluation_date=date)
    print(json.dumps({"job_id": job["id"], "deduplication_key": job["deduplication_key"], "status": job["status"]}, ensure_ascii=False))


def command_retention_work(limit: int) -> None:
    from app.database import get_connection
    from app.retention.scheduler import process_due_jobs

    # worker 不使用外层事务：评估内部按批次提交检查点
    processed = process_due_jobs(get_connection(), limit=limit)
    print(json.dumps({"processed": processed}, ensure_ascii=False, default=str))


def command_retention_run(date: str | None) -> None:
    """每日定时入口：入队当天评估并立即由本地 worker 执行（可从检查点恢复）。"""
    from app.database import get_connection, transaction
    from app.core.clock import SystemClock
    from app.retention.engine import RetentionEvaluationService
    from app.retention.scheduler import RetentionScheduler

    connection = get_connection()
    date_text = date or SystemClock().now().date().isoformat()
    with transaction(immediate=True):
        RetentionScheduler(connection).enqueue_daily(evaluation_date=date_text)
    result = RetentionEvaluationService(connection).run_for_date(date_text, started_by="cli-daily")
    evaluation = result["evaluation"]
    print(json.dumps(
        {
            "evaluation_date": date_text,
            "status": evaluation["status"],
            "total": evaluation["total_samples"],
            "included": evaluation["included_count"],
            "excluded": evaluation["excluded_count"],
            "deferred": evaluation["deferred_count"],
            "replayed": result.get("replayed", False),
        },
        ensure_ascii=False,
    ))


def main() -> None:
    parser = argparse.ArgumentParser(description="科研样品服务维护命令")
    parser.add_argument("command", choices=(
        "init-db", "check-db", "smoke",
        "retention-enqueue", "retention-work", "retention-run",
    ))
    parser.add_argument("--date", help="评估日期 YYYY-MM-DD（仅到期评估命令使用）")
    parser.add_argument("--limit", type=int, default=5, help="worker 单次最多处理任务数")
    args = parser.parse_args()
    if args.command == "init-db":
        command_init()
    elif args.command == "check-db":
        command_check()
    elif args.command == "smoke":
        command_smoke()
    elif args.command == "retention-enqueue":
        command_retention_enqueue(args.date)
    elif args.command == "retention-work":
        command_retention_work(args.limit)
    elif args.command == "retention-run":
        command_retention_run(args.date)


if __name__ == "__main__":
    main()
