from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.clock import FrozenClock, to_storage
from app.core.errors import ConflictError
from app.database import get_connection, transaction

OLD_RECEIPT = "2026-01-01T00:00:00+00:00"
RECENT_RECEIPT = "2026-03-01T00:00:00+00:00"


def make_user(client, admin, username, role_codes):
    password = "Test!234567"
    response = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": password, "display_name": username, "role_codes": role_codes},
    )
    assert response.status_code == 201, response.text
    login = client.post("/api/auth/login", json={"username": username, "password": password, "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"headers": {"Authorization": f"Bearer {login.json()['token']}"}, "id": response.json()["id"]}


def make_sample(client, admin, code, sample_type="土壤", project="P-ALPHA", quantity=100):
    location = client.post(
        "/api/samples/locations",
        headers=admin["headers"],
        json={
            "code": f"LOC-{code}",
            "building": "科研楼",
            "room": "常温库",
            "cabinet": "柜一",
            "shelf": "一层",
            "sensitivity": "normal",
            "capacity_units": 100,
        },
    )
    assert location.status_code == 201, location.text
    batch = client.post(
        "/api/samples/batches",
        headers=admin["headers"],
        json={"batch_code": f"BATCH-{code}", "project_code": project, "expected_count": 1},
    )
    assert batch.status_code == 201, batch.text
    sample = client.post(
        "/api/samples",
        headers=admin["headers"],
        json={
            "sample_code": code,
            "batch_id": batch.json()["id"],
            "sample_type": sample_type,
            "quantity": quantity,
            "unit": "g",
            "location_id": location.json()["id"],
        },
    )
    assert sample.status_code == 201, sample.text
    return batch.json(), sample.json()


def backdate_batch(batch_id, received_at=OLD_RECEIPT):
    with transaction(immediate=True) as connection:
        connection.execute("UPDATE receipt_batches SET received_at=? WHERE id=?", (received_at, batch_id))


def create_policy(client, admin, code="POL-SOIL", sample_type="土壤", months=6, project=None):
    payload = {"policy_code": code, "sample_type": sample_type, "retention_months": months, "description": "测试策略"}
    if project:
        payload["project_code"] = project
    response = client.post("/api/retention/policies", headers=admin["headers"], json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def run_daily(client, admin, **payload):
    response = client.post("/api/retention/runs", headers=admin["headers"], json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def current_evaluations(client, admin, **params):
    return client.get("/api/retention/evaluations", headers=admin["headers"], params=params).json()


def tomorrow():
    return (date.today() + timedelta(days=1)).isoformat()


def test_daily_run_flags_due_samples_and_is_idempotent(client, admin):
    create_policy(client, admin)
    batch, sample = make_sample(client, admin, "S-DUE-1")
    backdate_batch(batch["id"])

    first = run_daily(client, admin)
    assert first["replayed"] is False
    assert first["run"]["status"] == "completed"
    assert first["run"]["candidate_count"] == 1
    assert first["run"]["processed_count"] == 1

    evaluations = current_evaluations(client, admin)
    assert len(evaluations) == 1
    assert evaluations[0]["decision"] == "candidate"
    assert evaluations[0]["reasons"][0]["code"] == "retention_expired"
    assert evaluations[0]["policy_version"] == 1

    # 同一天重复运行：直接返回已完成任务，不重复建单
    again = run_daily(client, admin)
    assert again["replayed"] is True
    assert again["run"]["id"] == first["run"]["id"]

    # 另一天重跑：状态未变化，结论指纹一致，不新增评估记录
    next_day = run_daily(client, admin, run_date=tomorrow())
    assert next_day["run"]["unchanged_count"] == 1
    assert next_day["run"]["candidate_count"] == 0
    assert len(current_evaluations(client, admin)) == 1
    history = current_evaluations(client, admin, history=True)
    assert len(history) == 1


def test_not_yet_due_sample_is_deferred(client, admin):
    create_policy(client, admin)
    make_sample(client, admin, "S-FRESH")
    result = run_daily(client, admin)
    assert result["run"]["deferred_count"] == 1
    evaluation = current_evaluations(client, admin)[0]
    assert evaluation["decision"] == "deferred"
    assert evaluation["reasons"][0]["code"] == "not_yet_due"
    assert evaluation["retain_until"] is not None


def test_sample_without_policy_is_excluded(client, admin):
    batch, _ = make_sample(client, admin, "S-NOPOL", sample_type="未知类型")
    backdate_batch(batch["id"], "2020-01-01T00:00:00+00:00")
    result = run_daily(client, admin)
    assert result["run"]["excluded_count"] == 1
    evaluation = current_evaluations(client, admin)[0]
    assert evaluation["decision"] == "excluded"
    assert evaluation["reasons"][0]["code"] == "no_policy"


def test_project_specific_policy_overrides_type_default(client, admin):
    create_policy(client, admin, code="POL-SOIL-DEFAULT", months=60)
    create_policy(client, admin, code="POL-SOIL-PROJ", months=6, project="P-SHORT")
    b1, s1 = make_sample(client, admin, "S-P1", project="P-SHORT")
    b2, s2 = make_sample(client, admin, "S-P2", project="P-LONG")
    backdate_batch(b1["id"], RECENT_RECEIPT)
    backdate_batch(b2["id"], RECENT_RECEIPT)
    run_daily(client, admin)
    evaluations = {item["sample_id"]: item for item in current_evaluations(client, admin)}
    assert evaluations[s1["id"]]["decision"] == "candidate"
    assert evaluations[s2["id"]]["decision"] == "deferred"


def test_loan_anomaly_paper_ref_and_hold_keep_samples_off_candidates(client, admin):
    create_policy(client, admin)
    b1, s1 = make_sample(client, admin, "S-LOAN")
    b2, s2 = make_sample(client, admin, "S-ANOM")
    b3, s3 = make_sample(client, admin, "S-PAPER")
    b4, s4 = make_sample(client, admin, "S-HELD")
    for batch in (b1, b2, b3, b4):
        backdate_batch(batch["id"])

    loan = client.post(
        "/api/samples/loans",
        headers=admin["headers"],
        json={"sample_id": s1["id"], "borrower_user_id": admin["body"]["user"]["id"], "quantity": 10, "due_at": "2027-01-01T00:00:00+00:00"},
    )
    assert loan.status_code == 201, loan.text
    anomaly = client.post(
        "/api/samples/anomalies",
        headers=admin["headers"],
        json={"sample_id": s2["id"], "anomaly_type": "标签脱落", "severity": "medium", "description": "标签脱落需补贴"},
    )
    assert anomaly.status_code == 201, anomaly.text
    ref = client.post(
        "/api/retention/paper-refs",
        headers=admin["headers"],
        json={"sample_id": s3["id"], "publication": "doi:10.0000/test"},
    )
    assert ref.status_code == 201, ref.text
    hold = client.post(
        "/api/retention/legal-holds",
        headers=admin["headers"],
        json={"sample_id": s4["id"], "reason": "诉讼证据保全"},
    )
    assert hold.status_code == 201, hold.text

    result = run_daily(client, admin)
    assert result["run"]["candidate_count"] == 0
    evaluations = {item["sample_id"]: item for item in current_evaluations(client, admin)}
    assert evaluations[s1["id"]]["decision"] == "deferred"
    assert evaluations[s1["id"]]["reasons"][0]["code"] == "loan_active"
    assert evaluations[s2["id"]]["decision"] == "deferred"
    assert evaluations[s2["id"]]["reasons"][0]["code"] == "anomaly_open"
    assert evaluations[s3["id"]]["decision"] == "deferred"
    assert evaluations[s3["id"]]["reasons"][0]["code"] == "paper_ref_active"
    assert evaluations[s4["id"]]["decision"] == "excluded"
    assert evaluations[s4["id"]]["reasons"][0]["code"] == "legal_hold"


def test_legal_hold_on_child_covers_whole_lineage(client, admin):
    create_policy(client, admin)
    batch, parent = make_sample(client, admin, "S-PARENT")
    backdate_batch(batch["id"])
    aliquot = client.post(
        f"/api/samples/{parent['id']}/aliquots",
        headers=admin["headers"],
        json={"requested_quantity": 10, "children": [{"sample_code": "S-CHILD", "quantity": 10}]},
    )
    assert aliquot.status_code == 201, aliquot.text
    child = aliquot.json()["children"][0]
    hold = client.post(
        "/api/retention/legal-holds",
        headers=admin["headers"],
        json={"sample_id": child["id"], "reason": "司法取证"},
    )
    assert hold.status_code == 201, hold.text

    run_daily(client, admin)
    evaluations = {item["sample_id"]: item for item in current_evaluations(client, admin)}
    assert evaluations[parent["id"]]["decision"] == "excluded"
    assert evaluations[parent["id"]]["reasons"][0]["code"] == "legal_hold"
    assert evaluations[child["id"]]["decision"] == "excluded"


def test_policy_version_change_expires_old_conclusions(client, admin):
    create_policy(client, admin, months=6)
    batch, _ = make_sample(client, admin, "S-VER")
    backdate_batch(batch["id"], RECENT_RECEIPT)
    run_daily(client, admin)
    before = current_evaluations(client, admin)
    assert before[0]["decision"] == "candidate"
    assert before[0]["policy_version"] == 1

    versioned = client.post(
        "/api/retention/policies/POL-SOIL/versions",
        headers=admin["headers"],
        json={"retention_months": 36, "description": "保存期延长到三年"},
    )
    assert versioned.status_code == 201, versioned.text
    assert versioned.json()["version"] == 2
    assert versioned.json()["expired_evaluation_count"] == 1

    # 旧结论被标记过期而不是改写，当前结论暂时为空
    assert current_evaluations(client, admin) == []
    history = current_evaluations(client, admin, history=True)
    assert len(history) == 1
    assert history[0]["status"] == "superseded"
    assert history[0]["superseded_reason"] == "policy_changed"

    # 重新评估后按新版本得出未到期结论，历史行保留
    run_daily(client, admin, run_date=tomorrow())
    current = current_evaluations(client, admin)
    assert current[0]["decision"] == "deferred"
    assert current[0]["policy_version"] == 2
    assert len(current_evaluations(client, admin, history=True)) == 2


def test_run_resumes_from_checkpoint_without_duplicates(client, admin):
    from app.retention.service import RetentionEvaluationService
    from app.services.audit import AuditContext

    create_policy(client, admin)
    for code in ("S-C1", "S-C2", "S-C3"):
        batch, _ = make_sample(client, admin, code)
        backdate_batch(batch["id"])

    clock = FrozenClock(datetime(2026, 9, 25, 9, 0, tzinfo=UTC))
    service = RetentionEvaluationService(get_connection(), clock)
    actor = AuditContext(actor_user_id=None, actor_name="系统")

    first = service.run_daily(actor, "2026-09-25", batch_size=2, max_batches=1)
    assert first["replayed"] is False
    assert first["run"]["status"] == "running"
    assert first["run"]["processed_count"] == 2
    assert first["run"]["checkpoint_sample_id"] > 0

    second = service.run_daily(actor, "2026-09-25", batch_size=2)
    assert second["run"]["status"] == "completed"
    assert second["run"]["processed_count"] == 3
    assert second["run"]["candidate_count"] == 3

    count = get_connection().execute("SELECT COUNT(*) FROM retention_evaluations").fetchone()[0]
    assert count == 3

    third = service.run_daily(actor, "2026-09-25")
    assert third["replayed"] is True
    count = get_connection().execute("SELECT COUNT(*) FROM retention_evaluations").fetchone()[0]
    assert count == 3


def test_run_lease_blocks_concurrent_workers(client, admin):
    from app.retention.service import RetentionEvaluationService
    from app.services.audit import AuditContext

    create_policy(client, admin)
    batch, _ = make_sample(client, admin, "S-LEASE")
    backdate_batch(batch["id"])

    clock = FrozenClock(datetime(2026, 9, 25, 9, 0, tzinfo=UTC))
    service = RetentionEvaluationService(get_connection(), clock)
    actor = AuditContext(actor_user_id=None, actor_name="系统")
    first = service.run_daily(actor, "2026-09-25", batch_size=1, max_batches=1, worker="worker-a")
    assert first["run"]["status"] == "running"

    # 模拟另一个执行者仍持有未过期租约
    with transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE retention_runs SET locked_by='worker-b', locked_at=? WHERE run_key='retention-daily:2026-09-25'",
            (to_storage(clock.now()),),
        )
    with pytest.raises(ConflictError):
        service.run_daily(actor, "2026-09-25", worker="worker-a")


def test_extension_dual_approval_defers_destruction(client, admin):
    create_policy(client, admin)
    batch, sample = make_sample(client, admin, "S-EXT")
    backdate_batch(batch["id"])
    researcher = make_user(client, admin, "lead.one", ["researcher"])
    approver_one = make_user(client, admin, "approver.one", ["approver"])
    approver_two = make_user(client, admin, "approver.two", ["approver"])

    extend_until = (datetime.now(UTC) + timedelta(days=180)).isoformat()
    created = client.post(
        "/api/retention/extensions",
        headers=researcher["headers"],
        json={"sample_id": sample["id"], "extend_until": extend_until, "reason": "论文数据复核需要"},
    )
    assert created.status_code == 201, created.text
    extension = created.json()
    assert extension["state"] == "pending"
    request_id = extension["request_id"]

    # 待审批期间样品被推迟
    run_daily(client, admin)
    evaluation = current_evaluations(client, admin)[0]
    assert evaluation["decision"] == "deferred"
    assert "extension_pending" in [reason["code"] for reason in evaluation["reasons"]]

    # 申请人不能自批（研究人员没有审批权限），且需要两名不同审批人
    own = client.post(
        f"/api/samples/approvals/{request_id}/decisions",
        headers=researcher["headers"],
        json={"decision": "approve"},
    )
    assert own.status_code == 403
    first = client.post(
        f"/api/samples/approvals/{request_id}/decisions",
        headers=approver_one["headers"],
        json={"decision": "approve"},
    )
    assert first.status_code == 200, first.text
    pending = client.get("/api/retention/extensions", headers=admin["headers"], params={"sample_id": sample["id"]}).json()
    assert pending[0]["state"] == "pending"
    second = client.post(
        f"/api/samples/approvals/{request_id}/decisions",
        headers=approver_two["headers"],
        json={"decision": "approve"},
    )
    assert second.status_code == 200, second.text
    approved = client.get("/api/retention/extensions", headers=admin["headers"], params={"sample_id": sample["id"]}).json()
    assert approved[0]["state"] == "approved"

    # 批准的延期让到期样品继续推迟
    run_daily(client, admin, run_date=tomorrow())
    evaluation = current_evaluations(client, admin)[0]
    assert evaluation["decision"] == "deferred"
    assert "extension_active" in [reason["code"] for reason in evaluation["reasons"]]


def test_expired_extension_allows_candidate(client, admin):
    from app.retention.service import RetentionEvaluationService
    from app.services.audit import AuditContext

    create_policy(client, admin)
    batch, sample = make_sample(client, admin, "S-EXTEXP")
    backdate_batch(batch["id"])
    with transaction(immediate=True) as connection:
        connection.execute(
            """INSERT INTO approval_requests(request_code,action_type,resource_type,resource_id,requested_by,payload_json,state,required_approvals,expires_at,created_at,updated_at)
               VALUES('APR-EXTEXP','retention_extension','sample',?,1,'{}','approved',2,'2027-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')""",
            (sample["id"],),
        )
        request_id = connection.execute("SELECT id FROM approval_requests WHERE request_code='APR-EXTEXP'").fetchone()[0]
        connection.execute(
            """INSERT INTO retention_extensions(extension_code,sample_id,request_id,requested_by,extend_until,reason,state,created_at,updated_at)
               VALUES('EXT-EXP',?,?,1,'2026-08-01T00:00:00+00:00','短期延期','approved','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')""",
            (sample["id"], request_id),
        )

    clock = FrozenClock(datetime(2026, 9, 25, 9, 0, tzinfo=UTC))
    service = RetentionEvaluationService(get_connection(), clock)
    result = service.run_daily(AuditContext(actor_user_id=None, actor_name="系统"), "2026-09-25")
    assert result["run"]["candidate_count"] == 1


def test_report_and_grouped_destruction_plan_flow(client, admin):
    create_policy(client, admin)
    b1, s1 = make_sample(client, admin, "S-PLAN-1")
    b2, s2 = make_sample(client, admin, "S-PLAN-2")
    b3, s3 = make_sample(client, admin, "S-PLAN-3")
    for batch in (b1, b2, b3):
        backdate_batch(batch["id"])
    anomaly = client.post(
        "/api/samples/anomalies",
        headers=admin["headers"],
        json={"sample_id": s3["id"], "anomaly_type": "包装破损", "severity": "low", "description": "外包装破损待更换"},
    )
    assert anomaly.status_code == 201, anomaly.text
    run_daily(client, admin)

    report = client.post("/api/retention/reports", headers=admin["headers"], json={})
    assert report.status_code == 201, report.text
    body = report.json()
    assert body["report"]["candidate_count"] == 2
    assert body["report"]["deferred_count"] == 1
    # 报告解释纳入与推迟原因
    assert body["items"]["candidate"][0]["reasons"][0]["code"] == "retention_expired"
    assert body["items"]["deferred"][0]["reasons"][0]["code"] == "anomaly_open"

    plan = client.post("/api/retention/destruction-plans", headers=admin["headers"], json={"report_id": body["report"]["id"]})
    assert plan.status_code == 201, plan.text
    plan_body = plan.json()
    assert len(plan_body["items"]) == 2
    assert plan_body["skipped"] == []
    plan_id = plan_body["plan"]["id"]

    submitted = client.post(f"/api/retention/destruction-plans/{plan_id}/submit", headers=admin["headers"])
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["plan"]["state"] == "submitted"
    items = submitted.json()["items"]
    assert all(item["approval_request_id"] for item in items)

    approver_one = make_user(client, admin, "plan.approver1", ["approver"])
    approver_two = make_user(client, admin, "plan.approver2", ["approver"])
    for item in items:
        for approver in (approver_one, approver_two):
            decided = client.post(
                f"/api/samples/approvals/{item['approval_request_id']}/decisions",
                headers=approver["headers"],
                json={"decision": "approve"},
            )
            assert decided.status_code == 200, decided.text

    for index, item in enumerate(items):
        executed = client.post(
            f"/api/retention/destruction-plans/{plan_id}/items/{item['id']}/execute",
            headers=admin["headers"],
            json={"method": "高温焚烧", "witness_one": approver_one["id"], "witness_two": approver_two["id"]},
        )
        assert executed.status_code == 200, executed.text
    assert executed.json()["plan"]["state"] == "completed"

    detail = client.get(f"/api/samples/{items[0]['sample_id']}", headers=admin["headers"])
    assert detail.json()["lifecycle_state"] == "destroyed"


def test_plan_creation_skips_newly_blocked_candidates(client, admin):
    create_policy(client, admin)
    b1, s1 = make_sample(client, admin, "S-SAFE-1")
    b2, s2 = make_sample(client, admin, "S-SAFE-2")
    for batch in (b1, b2):
        backdate_batch(batch["id"])
    run_daily(client, admin)
    report = client.post("/api/retention/reports", headers=admin["headers"], json={}).json()

    # 报告生成后样品被借出，计划移交时必须实时复核并剔除
    loan = client.post(
        "/api/samples/loans",
        headers=admin["headers"],
        json={"sample_id": s2["id"], "borrower_user_id": admin["body"]["user"]["id"], "quantity": 5, "due_at": "2027-01-01T00:00:00+00:00"},
    )
    assert loan.status_code == 201, loan.text

    plan = client.post("/api/retention/destruction-plans", headers=admin["headers"], json={"report_id": report["report"]["id"]})
    assert plan.status_code == 201, plan.text
    body = plan.json()
    assert [item["sample_id"] for item in body["items"]] == [s1["id"]]
    assert body["skipped"][0]["sample_id"] == s2["id"]
    assert body["skipped"][0]["reasons"][0]["code"] == "loan_active"


def test_execute_blocked_when_hold_placed_after_approval(client, admin):
    create_policy(client, admin)
    batch, sample = make_sample(client, admin, "S-HOLD-EXEC")
    backdate_batch(batch["id"])
    run_daily(client, admin)
    report = client.post("/api/retention/reports", headers=admin["headers"], json={}).json()
    plan = client.post("/api/retention/destruction-plans", headers=admin["headers"], json={"report_id": report["report"]["id"]}).json()
    plan_id = plan["plan"]["id"]
    client.post(f"/api/retention/destruction-plans/{plan_id}/submit", headers=admin["headers"])
    approver_one = make_user(client, admin, "hold.approver1", ["approver"])
    approver_two = make_user(client, admin, "hold.approver2", ["approver"])
    item = client.get(f"/api/retention/destruction-plans/{plan_id}", headers=admin["headers"]).json()["items"][0]
    for approver in (approver_one, approver_two):
        decided = client.post(
            f"/api/samples/approvals/{item['approval_request_id']}/decisions",
            headers=approver["headers"],
            json={"decision": "approve"},
        )
        assert decided.status_code == 200, decided.text

    hold = client.post(
        "/api/retention/legal-holds",
        headers=admin["headers"],
        json={"sample_id": sample["id"], "reason": "临时证据保全"},
    )
    assert hold.status_code == 201, hold.text
    blocked = client.post(
        f"/api/retention/destruction-plans/{plan_id}/items/{item['id']}/execute",
        headers=admin["headers"],
        json={"method": "高温焚烧", "witness_one": approver_one["id"], "witness_two": approver_two["id"]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["context"]["problems"][0]["code"] == "legal_hold"

    released = client.post(
        f"/api/retention/legal-holds/{hold.json()['id']}/release",
        headers=admin["headers"],
        json={"note": "保全解除"},
    )
    assert released.status_code == 200, released.text
    executed = client.post(
        f"/api/retention/destruction-plans/{plan_id}/items/{item['id']}/execute",
        headers=admin["headers"],
        json={"method": "高温焚烧", "witness_one": approver_one["id"], "witness_two": approver_two["id"]},
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["plan"]["state"] == "completed"


def test_sample_in_active_plan_is_not_planned_twice(client, admin):
    create_policy(client, admin)
    batch, sample = make_sample(client, admin, "S-DUP-PLAN")
    backdate_batch(batch["id"])
    run_daily(client, admin)
    report = client.post("/api/retention/reports", headers=admin["headers"], json={}).json()
    first = client.post("/api/retention/destruction-plans", headers=admin["headers"], json={"report_id": report["report"]["id"]})
    assert first.status_code == 201, first.text
    assert len(first.json()["items"]) == 1

    # 样品已在未完成的计划中，第二个计划必须将其剔除并说明原因
    second = client.post("/api/retention/destruction-plans", headers=admin["headers"], json={"report_id": report["report"]["id"]})
    assert second.status_code == 409
    skipped = second.json()["error"]["context"]["skipped"]
    assert skipped[0]["sample_id"] == sample["id"]
    assert skipped[0]["reasons"][0]["code"] == "already_planned"


def test_retention_permissions_are_enforced(client, admin):
    researcher = make_user(client, admin, "perm.researcher", ["researcher"])
    denied_policy = client.post(
        "/api/retention/policies",
        headers=researcher["headers"],
        json={"policy_code": "POL-X", "sample_type": "土壤", "retention_months": 6},
    )
    assert denied_policy.status_code == 403
    denied_run = client.post("/api/retention/runs", headers=researcher["headers"], json={})
    assert denied_run.status_code == 403
    denied_report = client.post("/api/retention/reports", headers=researcher["headers"], json={})
    assert denied_report.status_code == 403


def test_approval_requests_migration_adds_retention_extension(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(str(db_path))
    legacy.execute(
        """CREATE TABLE approval_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_code TEXT NOT NULL UNIQUE,
            action_type TEXT NOT NULL CHECK(action_type IN ('loan','destruction','location_reveal','inventory_adjustment')),
            resource_type TEXT NOT NULL,
            resource_id INTEGER NOT NULL,
            requested_by INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            state TEXT NOT NULL,
            required_approvals INTEGER NOT NULL DEFAULT 2,
            expires_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    legacy.execute(
        """INSERT INTO approval_requests(request_code,action_type,resource_type,resource_id,requested_by,payload_json,state,required_approvals,expires_at,created_at,updated_at)
           VALUES('APR-OLD','loan','sample',1,1,'{}','pending',2,'2026-01-01','2026-01-01','2026-01-01')"""
    )
    legacy.commit()
    legacy.close()

    monkeypatch.setenv("SAMPLE_DATABASE_PATH", str(db_path))
    from app.database import close_connection, init_db

    close_connection()
    init_db()
    row = get_connection().execute("SELECT sql FROM sqlite_master WHERE name='approval_requests'").fetchone()
    assert "retention_extension" in row[0]
    kept = get_connection().execute("SELECT request_code FROM approval_requests").fetchall()
    assert [item[0] for item in kept] == ["APR-OLD"]
    close_connection()
