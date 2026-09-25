from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.retention.schemas import (
    DestructionItemExecute,
    DestructionPlanCreate,
    ExtensionCreate,
    LegalHoldCreate,
    LegalHoldRelease,
    PaperRefCreate,
    PolicyCreate,
    PolicyVersionCreate,
    ReportGenerate,
    RetentionRunRequest,
)
from app.retention.service import (
    DestructionPlanService,
    LegalHoldService,
    PaperReviewService,
    RetentionEvaluationService,
    RetentionExtensionService,
    RetentionPolicyService,
    RetentionReportService,
)

router = APIRouter(prefix="/api/retention", tags=["保存期与销毁"])


@router.post("/policies", status_code=status.HTTP_201_CREATED)
def create_policy(payload: PolicyCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return RetentionPolicyService(connection).create_line(principal, payload.model_dump())


@router.get("/policies")
def list_policies(principal: Principal = Depends(current_principal)):
    return RetentionPolicyService(get_connection()).list(principal)


@router.post("/policies/{policy_code}/versions", status_code=status.HTTP_201_CREATED)
def create_policy_version(policy_code: str, payload: PolicyVersionCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return RetentionPolicyService(connection).create_version(principal, policy_code, payload.model_dump())


@router.post("/runs", status_code=status.HTTP_201_CREATED)
def trigger_run(payload: RetentionRunRequest, principal: Principal = Depends(current_principal)):
    principal.require("jobs.run")
    # 评估任务按批提交事务并维护检查点，不能在路由层包裹大事务。
    return RetentionEvaluationService(get_connection()).run_daily(
        principal,
        payload.run_date,
        batch_size=payload.batch_size,
        max_batches=payload.max_batches,
        lease_seconds=payload.lease_seconds,
    )


@router.get("/runs")
def list_runs(principal: Principal = Depends(current_principal)):
    return RetentionEvaluationService(get_connection()).list_runs(principal)


@router.get("/runs/{run_id}")
def get_run(run_id: int, principal: Principal = Depends(current_principal)):
    principal.require("retention.read")
    return RetentionEvaluationService(get_connection()).get_run(run_id)


@router.get("/evaluations")
def list_evaluations(
    decision: str | None = Query(default=None),
    sample_id: int | None = Query(default=None),
    history: bool = Query(default=False),
    principal: Principal = Depends(current_principal),
):
    return RetentionEvaluationService(get_connection()).list_evaluations(
        principal, decision=decision, sample_id=sample_id, history=history
    )


@router.post("/extensions", status_code=status.HTTP_201_CREATED)
def request_extension(payload: ExtensionCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return RetentionExtensionService(connection).request(principal, payload.model_dump())


@router.get("/extensions")
def list_extensions(sample_id: int | None = Query(default=None), principal: Principal = Depends(current_principal)):
    return RetentionExtensionService(get_connection()).list(principal, sample_id)


@router.post("/legal-holds", status_code=status.HTTP_201_CREATED)
def place_hold(payload: LegalHoldCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LegalHoldService(connection).place(principal, payload.model_dump())


@router.get("/legal-holds")
def list_holds(active_only: bool = Query(default=True), principal: Principal = Depends(current_principal)):
    return LegalHoldService(get_connection()).list(principal, active_only)


@router.post("/legal-holds/{hold_id}/release")
def release_hold(hold_id: int, payload: LegalHoldRelease, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LegalHoldService(connection).release(principal, hold_id, payload.note)


@router.post("/paper-refs", status_code=status.HTTP_201_CREATED)
def add_paper_ref(payload: PaperRefCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return PaperReviewService(connection).add(principal, payload.model_dump())


@router.get("/paper-refs")
def list_paper_refs(active_only: bool = Query(default=True), principal: Principal = Depends(current_principal)):
    return PaperReviewService(get_connection()).list(principal, active_only)


@router.post("/paper-refs/{ref_id}/release")
def release_paper_ref(ref_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return PaperReviewService(connection).release(principal, ref_id)


@router.post("/reports", status_code=status.HTTP_201_CREATED)
def generate_report(payload: ReportGenerate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return RetentionReportService(connection).generate(principal, payload.run_id)


@router.get("/reports")
def list_reports(principal: Principal = Depends(current_principal)):
    return RetentionReportService(get_connection()).list(principal)


@router.get("/reports/{report_id}")
def get_report(report_id: int, principal: Principal = Depends(current_principal)):
    return RetentionReportService(get_connection()).detail(principal, report_id)


@router.post("/destruction-plans", status_code=status.HTTP_201_CREATED)
def create_plan(payload: DestructionPlanCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DestructionPlanService(connection).create_from_report(principal, payload.model_dump())


@router.get("/destruction-plans")
def list_plans(principal: Principal = Depends(current_principal)):
    return DestructionPlanService(get_connection()).list(principal)


@router.get("/destruction-plans/{plan_id}")
def get_plan(plan_id: int, principal: Principal = Depends(current_principal)):
    return DestructionPlanService(get_connection()).detail(principal, plan_id)


@router.post("/destruction-plans/{plan_id}/submit")
def submit_plan(plan_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DestructionPlanService(connection).submit(principal, plan_id)


@router.post("/destruction-plans/{plan_id}/items/{item_id}/execute")
def execute_plan_item(
    plan_id: int,
    item_id: int,
    payload: DestructionItemExecute,
    principal: Principal = Depends(current_principal),
):
    with transaction(immediate=True) as connection:
        return DestructionPlanService(connection).execute_item(principal, plan_id, item_id, payload.model_dump())
