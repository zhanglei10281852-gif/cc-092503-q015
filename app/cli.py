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


def command_retention_run(date: str | None, batch_size: int, max_batches: int | None) -> None:
    """每日保存期评估：可由 cron 每日调用，支持断点恢复与幂等重跑。"""
    init_db()
    from app.retention.service import RetentionEvaluationService
    from app.services.audit import AuditContext

    service = RetentionEvaluationService(get_connection())
    result = service.run_daily(
        AuditContext(actor_user_id=None, actor_name="系统"),
        date,
        batch_size=batch_size,
        max_batches=max_batches,
    )
    run = result["run"]
    print(
        json.dumps(
            {
                "run_key": run["run_key"],
                "status": run["status"],
                "checkpoint_sample_id": run["checkpoint_sample_id"],
                "processed_count": run["processed_count"],
                "candidate_count": run["candidate_count"],
                "deferred_count": run["deferred_count"],
                "excluded_count": run["excluded_count"],
                "unchanged_count": run["unchanged_count"],
                "replayed": result["replayed"],
            },
            ensure_ascii=False,
        )
    )
    if run["status"] == "failed":
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="科研样品服务维护命令")
    parser.add_argument("command", choices=("init-db", "check-db", "smoke", "retention-run"))
    parser.add_argument("--date", help="评估日期（YYYY-MM-DD），默认为今天", default=None)
    parser.add_argument("--batch-size", type=int, default=200, help="每批处理的样品数")
    parser.add_argument("--max-batches", type=int, default=None, help="本次最多处理的批数（用于时间片控制）")
    args = parser.parse_args()
    if args.command == "retention-run":
        command_retention_run(args.date, args.batch_size, args.max_batches)
        return
    {"init-db": command_init, "check-db": command_check, "smoke": command_smoke}[args.command]()


if __name__ == "__main__":
    main()
