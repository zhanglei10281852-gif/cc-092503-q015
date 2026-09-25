from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock, to_storage
from app.database import get_connection
from app.retention.engine import RetentionEvaluationService
from app.retention.scheduler import RetentionScheduler, process_due_jobs


def login(client, username, password):
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password, "client_label": "tests"},
    )
    assert response.status_code == 200, response.text
    return {"headers": {"Authorization": f"Bearer {response.json()['token']}"}}


def make_user(client, admin, username, roles, password="User!123456"):
    created = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": password, "display_name": username, "role_codes": roles},
    )
    assert created.status_code == 201, created.text
    return login(client, username, password)


def make_sample(client, admin, code, *, batch_code, project_code="P-RET", sample_type="土壤", quantity=10, location=None):
    batch = client.post(
        "/api/samples/batches",
        headers=admin["headers"],
        json={"batch_code": batch_code, "project_code": project_code, "expected_count": 5},
    )
    assert batch.status_code == 201, batch.text
    payload = {
        "sample_code": code,
        "batch_id": batch.json()["id"],
        "sample_type": sample_type,
        "quantity": quantity,
        "unit": "g",
    }
    if location is not None:
        payload["location_id"] = location
    sample = client.post("/api/samples", headers=admin["headers"], json=payload)
    assert sample.status_code == 201, sample.text
    return batch.json(), sample.json()


def backdate_batch(batch_id, days_ago, *, clock_dt=None):
    base = clock_dt or datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
    received = to_storage(base - timedelta(days=days_ago))
    connection = get_connection()
    connection.execute("UPDATE receipt_batches SET received_at=? WHERE id=?", (received, batch_id))
    connection.commit()


@pytest.fixture()
def location(client, admin):
    response = client.post(
        "/api/samples/locations",
        headers=admin["headers"],
        json={
            "code": "RET-LOC-1", "building": "样品楼", "room": "常温库",
            "cabinet": "柜一", "shelf": "一层", "sensitivity": "normal", "capacity_units": 100,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def frozen_now():
    return datetime(2026, 9, 25, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def freeze_system_clock(monkeypatch, request):
    """统一冻结系统时钟，避免 API 写入的真实时间与评估用 FrozenClock 不一致。"""
    if "frozen_now" not in request.fixturenames:
        yield
        return
    fixed = request.getfixturevalue("frozen_now")

    def fixed_now(self):  # noqa: ANN001
        return fixed

    monkeypatch.setattr("app.core.clock.SystemClock.now", fixed_now)
    yield


def run_eval(frozen_now, date_text="2026-09-25"):
    return RetentionEvaluationService(get_connection(), FrozenClock(frozen_now)).run_for_date(date_text)


# ---------------------------------------------------------------------- 策略版本化
def test_seed_global_policy_exists_and_can_version(client, admin):
    versions = client.get("/api/retention/policies/versions", headers=admin["headers"])
    assert versions.status_code == 200
    globals_ = [v for v in versions.json() if v["scope_type"] == "global"]
    assert globals_[0]["version"] == 1 and globals_[0]["retain_days"] == 365

    created = client.post(
        "/api/retention/policies/versions",
        headers=admin["headers"],
        json={
            "scope_type": "global", "retain_days": 730,
            "basis_text": "新版机构规定", "change_reason": "统一延长至两年",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["policy"]["version"] == 2

    rows = client.get("/api/retention/policies/versions?scope_type=global", headers=admin["headers"]).json()
    assert {r["status"] for r in rows} == {"active", "superseded"}
    # 历史行不被改写：v1 仍保留原始天数
    v1 = [r for r in rows if r["version"] == 1][0]
    assert v1["retain_days"] == 365 and v1["status"] == "superseded"


# ---------------------------------------------------------------------- 纳入/排除/推迟
def test_evaluation_classifies_included_not_due_and_loan(client, admin, location, frozen_now):
    _, old_sample = make_sample(client, admin, "RET-OLD", batch_code="RB-OLD", location=location["id"])
    _, new_sample = make_sample(client, admin, "RET-NEW", batch_code="RB-NEW", location=location["id"])
    backdate_batch(old_sample["batch_id"], 400, clock_dt=frozen_now)
    backdate_batch(new_sample["batch_id"], 10, clock_dt=frozen_now)

    # 老样品正在借用 → 推迟
    loan = client.post(
        "/api/samples/loans",
        headers=admin["headers"],
        json={
            "sample_id": old_sample["id"], "borrower_user_id": 1,
            "quantity": 1, "due_at": "2026-12-31T00:00:00+00:00",
        },
    )
    assert loan.status_code == 201, loan.text
    # 借用记录把样品置为 loaned；再登记一个已到期但无阻塞的样品
    _, free_old = make_sample(client, admin, "RET-FREE", batch_code="RB-FREE", location=location["id"])
    backdate_batch(free_old["batch_id"], 400, clock_dt=frozen_now)

    result = run_eval(frozen_now)
    assert result["evaluation"]["status"] == "completed"

    report = client.get(
        f"/api/retention/evaluations/{result['evaluation']['id']}/report", headers=admin["headers"]
    ).json()
    by_code = {c["sample_code"]: c for c in report["included"] + report["excluded"] + report["deferred"]}
    assert by_code["RET-FREE"]["outcome"] == "included"
    assert by_code["RET-FREE"]["reason_code"] == "due"
    assert by_code["RET-NEW"]["outcome"] == "excluded"
    assert by_code["RET-NEW"]["reason_code"] == "not_due"
    assert by_code["RET-OLD"]["outcome"] == "deferred"
    assert by_code["RET-OLD"]["reason_code"] == "on_loan"
    assert by_code["RET-OLD"]["blocking_loan_ids"]
    assert report["summary"]["included"] >= 1


def test_evaluation_defers_anomaly_review_and_hold(client, admin, location, frozen_now):
    _, anomaly_sample = make_sample(client, admin, "RET-ANM", batch_code="RB-ANM", location=location["id"])
    _, review_sample = make_sample(client, admin, "RET-REV", batch_code="RB-REV", location=location["id"])
    _, hold_sample = make_sample(client, admin, "RET-HOLD", batch_code="RB-HOLD", location=location["id"])
    for sample in (anomaly_sample, review_sample, hold_sample):
        backdate_batch(sample["batch_id"], 400, clock_dt=frozen_now)

    anomaly = client.post(
        "/api/samples/anomalies",
        headers=admin["headers"],
        json={
            "sample_id": anomaly_sample["id"], "anomaly_type": "包装破损",
            "severity": "high", "description": "待核查的破损异常",
        },
    )
    assert anomaly.status_code == 201, anomaly.text
    review = client.post(
        "/api/retention/publication-reviews",
        headers=admin["headers"],
        json={"sample_id": review_sample["id"], "publication_code": "PAPER-2026-001", "title": "复核中的论文"},
    )
    assert review.status_code == 201, review.text
    hold = client.post(
        "/api/retention/legal-holds",
        headers=admin["headers"],
        json={"scope_type": "sample", "sample_id": hold_sample["id"], "reason": "涉及司法协助的法律保留"},
    )
    assert hold.status_code == 201, hold.text

    result = run_eval(frozen_now)
    report = client.get(
        f"/api/retention/evaluations/{result['evaluation']['id']}/report", headers=admin["headers"]
    ).json()
    by_code = {c["sample_code"]: c for c in report["deferred"]}
    assert by_code["RET-ANM"]["reason_code"] == "anomaly_open"
    assert by_code["RET-REV"]["reason_code"] == "paper_review"
    assert by_code["RET-HOLD"]["reason_code"] == "legal_hold"


def test_batch_level_anomaly_defers_all_samples(client, admin, location, frozen_now):
    batch, sample = make_sample(client, admin, "RET-BANM", batch_code="RB-BANM", location=location["id"])
    backdate_batch(sample["batch_id"], 400, clock_dt=frozen_now)
    response = client.post(
        "/api/samples/anomalies",
        headers=admin["headers"],
        json={
            "batch_id": batch["id"], "anomaly_type": "接收单据缺失",
            "severity": "medium", "description": "批次级异常影响整批复核",
        },
    )
    assert response.status_code == 201, response.text
    result = run_eval(frozen_now)
    report = client.get(
        f"/api/retention/evaluations/{result['evaluation']['id']}/report", headers=admin["headers"]
    ).json()
    deferred = {c["sample_code"]: c for c in report["deferred"]}
    assert "RET-BANM" in deferred


# ---------------------------------------------------------------------- 重复运行幂等
def test_repeated_evaluation_does_not_duplicate(client, admin, location, frozen_now):
    _, sample = make_sample(client, admin, "RET-IDEM", batch_code="RB-IDEM", location=location["id"])
    backdate_batch(sample["batch_id"], 400, clock_dt=frozen_now)

    first = run_eval(frozen_now)
    second = run_eval(frozen_now)
    assert first["evaluation"]["id"] == second["evaluation"]["id"]
    assert second["replayed"] is True

    connection = get_connection()
    runs = connection.execute("SELECT COUNT(*) FROM retention_evaluations").fetchone()[0]
    conclusions = connection.execute(
        "SELECT COUNT(*) FROM retention_conclusions WHERE sample_id=?", (sample["id"],)
    ).fetchone()[0]
    assert runs == 1 and conclusions == 1


# ---------------------------------------------------------------------- 检查点恢复
def test_checkpoint_resume_after_failure(client, admin, location, frozen_now, monkeypatch):
    monkeypatch.setenv("SAMPLE_RETENTION_BATCH_SIZE", "1")
    samples = []
    for index in range(4):
        _, sample = make_sample(
            client, admin, f"RET-CP{index}", batch_code=f"RB-CP{index}", location=location["id"]
        )
        backdate_batch(sample["batch_id"], 400, clock_dt=frozen_now)
        samples.append(sample)

    connection = get_connection()
    service = RetentionEvaluationService(connection, FrozenClock(frozen_now))
    calls = {"count": 0}
    original = service._classify

    def flaky(sample, planned_ids, now_dt, now_text):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("模拟评估进程中断")
        return original(sample, planned_ids, now_dt, now_text)

    monkeypatch.setattr(service, "_classify", flaky)
    with pytest.raises(RuntimeError):
        service.run_for_date("2026-09-25")

    failed = connection.execute(
        "SELECT status,checkpoint_json FROM retention_evaluations WHERE evaluation_date='2026-09-25'"
    ).fetchone()
    assert failed["status"] == "failed"
    # 第一批的检查点已经落盘
    assert failed["checkpoint_json"] != "{}"

    # 恢复执行：新服务实例，从检查点继续，不重复处理已完成样品
    recovered = RetentionEvaluationService(connection, FrozenClock(frozen_now)).run_for_date("2026-09-25")
    assert recovered["evaluation"]["status"] == "completed"
    assert recovered["resumed"] is True
    total_conclusions = connection.execute(
        "SELECT COUNT(*) FROM retention_conclusions WHERE evaluation_id=?",
        (recovered["evaluation"]["id"],),
    ).fetchone()[0]
    assert total_conclusions == len(samples)
    assert recovered["evaluation"]["processed_samples"] == len(samples)


# ---------------------------------------------------------------------- 策略变更过期
def test_policy_change_marks_old_conclusions_stale(client, admin, location, frozen_now):
    _, sample = make_sample(client, admin, "RET-STALE", batch_code="RB-STALE", location=location["id"])
    backdate_batch(sample["batch_id"], 400, clock_dt=frozen_now)
    result = run_eval(frozen_now)

    updated = client.post(
        "/api/retention/policies/versions",
        headers=admin["headers"],
        json={
            "scope_type": "global", "retain_days": 3650,
            "basis_text": "长期留存", "change_reason": "监管要求十年",
        },
    )
    assert updated.status_code == 201
    assert updated.json()["stale_conclusions"] >= 1

    report = client.get(
        f"/api/retention/evaluations/{result['evaluation']['id']}/report", headers=admin["headers"]
    ).json()
    conclusion = [c for c in report["included"] + report["excluded"] + report["deferred"]
                  if c["sample_code"] == "RET-STALE"][0]
    assert conclusion["stale"] == 1
    assert conclusion["stale_reason"]

    # 旧报告不能再交给销毁计划：指纹不一致
    blocked = client.post("/api/retention/destruction-plans", headers=admin["headers"], json={})
    assert blocked.status_code == 409


# ---------------------------------------------------------------------- 延期双人审批
def test_extension_requires_two_distinct_approvers(client, admin, location, frozen_now):
    researcher = make_user(client, admin, "scientist1", ["researcher"])
    approver_one = make_user(client, admin, "approver1", ["approver"])
    approver_two = make_user(client, admin, "approver2", ["approver"])

    _, sample = make_sample(client, admin, "RET-EXT", batch_code="RB-EXT", location=location["id"])
    backdate_batch(sample["batch_id"], 400, clock_dt=frozen_now)

    requested = client.post(
        "/api/retention/extensions",
        headers=researcher["headers"],
        json={"sample_id": sample["id"], "reason": "补充实验需要继续留样", "extra_days": 180},
    )
    assert requested.status_code == 201, requested.text
    extension_id = requested.json()["id"]

    # 研究人员没有审批权限
    forbidden = client.post(
        f"/api/retention/extensions/{extension_id}/decisions",
        headers=researcher["headers"],
        json={"decision": "approve"},
    )
    assert forbidden.status_code == 403

    # 申请人不能自审：用拥有审批权的管理员代替申请人发起一份新申请
    own_request = client.post(
        "/api/retention/extensions",
        headers=admin["headers"],
        json={"sample_id": sample["id"], "reason": "管理员发起的延期自审检验", "extra_days": 30},
    )
    own_id = own_request.json()["id"]
    self_approval = client.post(
        f"/api/retention/extensions/{own_id}/decisions",
        headers=admin["headers"],
        json={"decision": "approve"},
    )
    assert self_approval.status_code == 409

    first = client.post(
        f"/api/retention/extensions/{extension_id}/decisions",
        headers=approver_one["headers"],
        json={"decision": "approve", "comment": "同意"},
    )
    assert first.status_code == 200
    assert first.json()["state"] == "pending"

    # 同一审批人不能累计两次
    duplicate = client.post(
        f"/api/retention/extensions/{extension_id}/decisions",
        headers=approver_one["headers"],
        json={"decision": "approve"},
    )
    assert duplicate.status_code in (409, 500)

    second = client.post(
        f"/api/retention/extensions/{extension_id}/decisions",
        headers=approver_two["headers"],
        json={"decision": "approve", "comment": "批准"},
    )
    assert second.status_code == 200
    assert second.json()["state"] == "approved"

    result = run_eval(frozen_now)
    report = client.get(
        f"/api/retention/evaluations/{result['evaluation']['id']}/report", headers=admin["headers"]
    ).json()
    conclusion = [c for c in report["included"] + report["excluded"] + report["deferred"]
                  if c["sample_code"] == "RET-EXT"][0]
    # 400 天已收样 + 180 天延期 → 到期日推到 545 天，尚未到期
    assert conclusion["outcome"] == "excluded"
    assert conclusion["reason_code"] == "not_due"
    assert len(conclusion["applied_extension_ids"]) == 1


def test_extension_rejection_keeps_sample_due(client, admin, location, frozen_now):
    researcher = make_user(client, admin, "scientist2", ["researcher"])
    approver_one = make_user(client, admin, "approver3", ["approver"])
    approver_two = make_user(client, admin, "approver4", ["approver"])

    _, sample = make_sample(client, admin, "RET-EXR", batch_code="RB-EXR", location=location["id"])
    backdate_batch(sample["batch_id"], 400, clock_dt=frozen_now)
    extension_id = client.post(
        "/api/retention/extensions",
        headers=researcher["headers"],
        json={"sample_id": sample["id"], "reason": "理由不够充分的延期", "extra_days": 30},
    ).json()["id"]
    client.post(
        f"/api/retention/extensions/{extension_id}/decisions",
        headers=approver_one["headers"], json={"decision": "approve"},
    )
    rejected = client.post(
        f"/api/retention/extensions/{extension_id}/decisions",
        headers=approver_two["headers"], json={"decision": "reject", "comment": "无必要"},
    )
    assert rejected.json()["state"] == "rejected"

    run_eval(frozen_now)
    connection = get_connection()
    row = connection.execute(
        """SELECT c.outcome,c.reason_code FROM retention_conclusions c
           JOIN samples s ON s.id=c.sample_id WHERE s.sample_code='RET-EXR'""",
    ).fetchone()
    assert row["outcome"] == "included" and row["reason_code"] == "due"


# ---------------------------------------------------------------------- 谱系保护
def test_lineage_review_holds_parent(client, admin, location, frozen_now):
    _, parent = make_sample(client, admin, "RET-PARENT", batch_code="RB-PAR", location=location["id"])
    backdate_batch(parent["batch_id"], 400, clock_dt=frozen_now)
    aliquot = client.post(
        f"/api/samples/{parent['id']}/aliquots",
        headers=admin["headers"],
        json={
            "requested_quantity": 2,
            "children": [{"sample_code": "RET-CHILD", "quantity": 2, "location_id": location["id"]}],
        },
    )
    assert aliquot.status_code == 201, aliquot.text
    child_id = aliquot.json()["children"][0]["id"]
    review = client.post(
        "/api/retention/publication-reviews",
        headers=admin["headers"],
        json={"sample_id": child_id, "publication_code": "PAPER-LINEAGE"},
    )
    assert review.status_code == 201

    result = run_eval(frozen_now)
    report = client.get(
        f"/api/retention/evaluations/{result['evaluation']['id']}/report", headers=admin["headers"]
    ).json()
    parent_conclusion = [c for c in report["deferred"] if c["sample_code"] == "RET-PARENT"][0]
    assert parent_conclusion["reason_code"] == "lineage_dependent"
    assert parent_conclusion["blocking_review_ids"]


# ---------------------------------------------------------------------- 成组销毁计划
def test_destruction_plan_is_idempotent_and_safe(client, admin, location, frozen_now):
    _, due_one = make_sample(client, admin, "RET-DUE1", batch_code="RB-DUE1", location=location["id"])
    _, due_two = make_sample(client, admin, "RET-DUE2", batch_code="RB-DUE2", location=location["id"])
    backdate_batch(due_one["batch_id"], 400, clock_dt=frozen_now)
    backdate_batch(due_two["batch_id"], 400, clock_dt=frozen_now)
    run_eval(frozen_now)

    plan = client.post(
        "/api/retention/destruction-plans",
        headers=admin["headers"],
        json={"batch_code": "DEST-PLAN-1", "note": "四季度首批"},
    )
    assert plan.status_code == 201, plan.text
    plan_body = plan.json()
    assert plan_body["sample_count"] == 2
    assert len(plan_body["items"]) == 2

    # 重复建单：两个候选都已入计划，拒绝重复
    duplicate = client.post("/api/retention/destruction-plans", headers=admin["headers"], json={})
    assert duplicate.status_code == 409

    # 新样品到期后再评估，新计划只含新候选，不会重复纳入旧样品
    _, due_three = make_sample(client, admin, "RET-DUE3", batch_code="RB-DUE3", location=location["id"])
    backdate_batch(due_three["batch_id"], 500, clock_dt=frozen_now)
    run_eval(frozen_now, date_text="2026-09-26")
    second_plan = client.post(
        "/api/retention/destruction-plans", headers=admin["headers"], json={"batch_code": "DEST-PLAN-2"}
    )
    assert second_plan.status_code == 201
    codes = {item["sample_code"] for item in second_plan.json()["items"]}
    assert codes == {"RET-DUE3"}

    # 发布
    released = client.post(
        f"/api/retention/destruction-plans/{plan_body['id']}/release", headers=admin["headers"]
    )
    assert released.status_code == 200
    assert released.json()["state"] == "released"


def test_destruction_plan_rejects_sample_with_new_loan(client, admin, location, frozen_now):
    _, sample = make_sample(client, admin, "RET-SAFE", batch_code="RB-SAFE", location=location["id"])
    backdate_batch(sample["batch_id"], 400, clock_dt=frozen_now)
    run_eval(frozen_now)

    # 评估之后才发生借用：交出计划前必须再次核验拦截
    loan = client.post(
        "/api/samples/loans",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"], "borrower_user_id": 1,
            "quantity": 1, "due_at": "2026-12-31T00:00:00+00:00",
        },
    )
    assert loan.status_code == 201
    blocked = client.post("/api/retention/destruction-plans", headers=admin["headers"], json={})
    assert blocked.status_code == 409
    assert blocked.json()["error"]["context"]["blocked"][0]["reason_codes"] == ["on_loan"]


def test_destruction_plan_rejects_non_candidate(client, admin, location, frozen_now):
    _, due = make_sample(client, admin, "RET-SEL1", batch_code="RB-SEL1", location=location["id"])
    _, fresh = make_sample(client, admin, "RET-SEL2", batch_code="RB-SEL2", location=location["id"])
    backdate_batch(due["batch_id"], 400, clock_dt=frozen_now)
    backdate_batch(fresh["batch_id"], 5, clock_dt=frozen_now)
    run_eval(frozen_now)
    response = client.post(
        "/api/retention/destruction-plans",
        headers=admin["headers"],
        json={"sample_ids": [due["id"], fresh["id"]]},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------- 项目策略优先
def test_project_policy_overrides_global(client, admin, location, frozen_now):
    _, sample = make_sample(
        client, admin, "RET-PRJ", batch_code="RB-PRJ", project_code="P-SPECIAL", location=location["id"]
    )
    backdate_batch(sample["batch_id"], 400, clock_dt=frozen_now)
    created = client.post(
        "/api/retention/policies/versions",
        headers=admin["headers"],
        json={"scope_type": "project", "scope_value": "P-SPECIAL", "retain_days": 730, "change_reason": "合同约定"},
    )
    assert created.status_code == 201, created.text
    run_eval(frozen_now)
    connection = get_connection()
    row = connection.execute(
        """SELECT c.outcome,c.reason_code,p.scope_type FROM retention_conclusions c
           JOIN samples s ON s.id=c.sample_id
           JOIN retention_policy_versions p ON p.id=c.policy_version_id
           WHERE s.sample_code='RET-PRJ'""",
    ).fetchone()
    assert row["outcome"] == "excluded" and row["reason_code"] == "not_due" and row["scope_type"] == "project"


# ---------------------------------------------------------------------- 法律保留解除
def test_releasing_hold_allows_destruction(client, admin, location, frozen_now):
    _, sample = make_sample(client, admin, "RET-REL", batch_code="RB-REL", location=location["id"])
    backdate_batch(sample["batch_id"], 400, clock_dt=frozen_now)
    hold_id = client.post(
        "/api/retention/legal-holds",
        headers=admin["headers"],
        json={"scope_type": "sample", "sample_id": sample["id"], "reason": "诉讼相关样品保留"},
    ).json()["id"]
    run_eval(frozen_now)
    connection = get_connection()
    before = connection.execute(
        "SELECT outcome FROM retention_conclusions c JOIN samples s ON s.id=c.sample_id WHERE s.sample_code='RET-REL'"
    ).fetchone()
    assert before["outcome"] == "deferred"

    released = client.post(
        f"/api/retention/legal-holds/{hold_id}/release",
        headers=admin["headers"],
        json={"reason": "诉讼结束"},
    )
    assert released.status_code == 200
    RetentionEvaluationService(connection, FrozenClock(frozen_now)).run_for_date("2026-09-26")
    after = connection.execute(
        """SELECT c.outcome FROM retention_conclusions c
           JOIN retention_evaluations e ON e.id=c.evaluation_id
           JOIN samples s ON s.id=c.sample_id
           WHERE s.sample_code='RET-REL' AND e.evaluation_date='2026-09-26'""",
    ).fetchone()
    assert after["outcome"] == "included"


# ---------------------------------------------------------------------- 调度去重
def test_scheduler_enqueue_is_deduplicated(client, admin, location, frozen_now):
    connection = get_connection()
    scheduler = RetentionScheduler(connection, FrozenClock(frozen_now))
    first = scheduler.enqueue_daily(evaluation_date="2026-09-25")
    second = scheduler.enqueue_daily(evaluation_date="2026-09-25")
    assert first["id"] == second["id"]

    _, sample = make_sample(client, admin, "RET-JOB", batch_code="RB-JOB", location=location["id"])
    backdate_batch(sample["batch_id"], 400, clock_dt=frozen_now)

    processed = process_due_jobs(connection, clock=FrozenClock(frozen_now))
    assert len(processed) == 1
    assert processed[0]["status"] == "completed"

    # 再跑 worker：任务已完成，无新增任务
    again = process_due_jobs(connection, clock=FrozenClock(frozen_now))
    assert again == []
