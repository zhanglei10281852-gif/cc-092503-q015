from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import current_principal
from app.core.clock import SystemClock
from app.core.security import Principal
from app.database import get_connection, transaction
from app.retention.engine import RetentionEvaluationService
from app.retention.schemas import (
    DestructionPlanCreate,
    EvaluationRunRequest,
    ExtensionDecision,
    ExtensionRequest,
    LegalHoldCreate,
    LegalHoldRelease,
    PublicationReviewCreate,
    PublicationReviewState,
    PolicyVersionCreate,
)
from app.retention.scheduler import RetentionScheduler
from app.retention.service import (
    DestructionPlanningService,
    LegalHoldService,
    PublicationReviewService,
    RetentionExtensionService,
    RetentionPolicyService,
)

router = APIRouter(prefix="/api/retention", tags=["保存期限与到期评估"])


# ---------------------------------------------------------------------- 保存策略
@router.post("/policies/versions", status_code=status.HTTP_201_CREATED)
def create_policy_version(payload: PolicyVersionCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return RetentionPolicyService(connection).create_version(principal, payload.model_dump())


@router.get("/policies/versions")
def list_policy_versions(
    scope_type: str | None = Query(default=None),
    scope_value: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    return RetentionPolicyService(get_connection()).list_versions(principal, scope_type, scope_value)


# ---------------------------------------------------------------------- 法律保留
@router.post("/legal-holds", status_code=status.HTTP_201_CREATED)
def create_legal_hold(payload: LegalHoldCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LegalHoldService(connection).create(principal, payload.model_dump())


@router.post("/legal-holds/{hold_id}/release")
def release_legal_hold(hold_id: int, payload: LegalHoldRelease, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LegalHoldService(connection).release(principal, hold_id, payload.reason)


@router.get("/legal-holds")
def list_legal_holds(active_only: bool = Query(default=False), principal: Principal = Depends(current_principal)):
    return LegalHoldService(get_connection()).list(principal, active_only)


# ---------------------------------------------------------------------- 论文复核
@router.post("/publication-reviews", status_code=status.HTTP_201_CREATED)
def register_publication_review(payload: PublicationReviewCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return PublicationReviewService(connection).register(principal, payload.model_dump())


@router.post("/publication-reviews/{review_id}/state")
def update_publication_review_state(review_id: int, payload: PublicationReviewState, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return PublicationReviewService(connection).update_state(principal, review_id, payload.review_state)


@router.get("/samples/{sample_id}/publication-reviews")
def list_publication_reviews(sample_id: int, principal: Principal = Depends(current_principal)):
    return PublicationReviewService(get_connection()).list_for_sample(principal, sample_id)


# ---------------------------------------------------------------------- 保留延期
@router.post("/extensions", status_code=status.HTTP_201_CREATED)
def request_extension(payload: ExtensionRequest, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return RetentionExtensionService(connection).request(principal, payload.model_dump())


@router.post("/extensions/{extension_id}/decisions")
def decide_extension(extension_id: int, payload: ExtensionDecision, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return RetentionExtensionService(connection).decide(principal, extension_id, payload.model_dump())


@router.get("/extensions")
def list_extensions(
    state: str | None = Query(default=None),
    sample_id: int | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    return RetentionExtensionService(get_connection()).list(principal, state, sample_id)


# ---------------------------------------------------------------------- 每日评估
@router.post("/evaluations/enqueue", status_code=status.HTTP_201_CREATED)
def enqueue_evaluation(payload: EvaluationRunRequest, principal: Principal = Depends(current_principal)):
    principal.require("retention.evaluate")
    with transaction(immediate=True) as connection:
        return RetentionScheduler(connection).enqueue_daily(evaluation_date=payload.evaluation_date)


@router.post("/evaluations/run")
def run_evaluation(payload: EvaluationRunRequest, principal: Principal = Depends(current_principal)):
    """同步执行（或从检查点恢复）当日评估，供授权人员手动触发与联调。

    评估自行按批次提交检查点，因此不在请求级事务内包裹。
    """
    principal.require("retention.evaluate")
    date_text = payload.evaluation_date or SystemClock().now().date().isoformat()
    return RetentionEvaluationService(get_connection()).run_for_date(
        date_text, started_by=f"user:{principal.username}"
    )


@router.get("/evaluations")
def list_evaluations(principal: Principal = Depends(current_principal)):
    principal.require("retention.report.read")
    from app.retention.repository import EvaluationRepository

    return EvaluationRepository(get_connection()).list_runs()


@router.get("/evaluations/{evaluation_id}/report")
def evaluation_report(evaluation_id: int, principal: Principal = Depends(current_principal)):
    principal.require("retention.report.read")
    return RetentionEvaluationService(get_connection()).report(evaluation_id)


@router.get("/candidates/latest")
def latest_candidate_report(principal: Principal = Depends(current_principal)):
    """最终候选报告：解释每个样品的纳入、排除与推迟原因。"""
    principal.require("retention.report.read")
    return RetentionEvaluationService(get_connection()).latest_report()


# ---------------------------------------------------------------------- 成组销毁计划
@router.post("/destruction-plans", status_code=status.HTTP_201_CREATED)
def create_destruction_plan(payload: DestructionPlanCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DestructionPlanningService(connection).plan(principal, payload.model_dump())


@router.post("/destruction-plans/{batch_id}/release")
def release_destruction_plan(batch_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DestructionPlanningService(connection).release(principal, batch_id)


@router.get("/destruction-plans")
def list_destruction_plans(principal: Principal = Depends(current_principal)):
    return DestructionPlanningService(get_connection()).list_batches(principal)


@router.get("/destruction-plans/{batch_id}")
def get_destruction_plan(batch_id: int, principal: Principal = Depends(current_principal)):
    return DestructionPlanningService(get_connection()).get(principal, batch_id)
