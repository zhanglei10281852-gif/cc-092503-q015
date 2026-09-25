from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class PolicyVersionCreate(BaseModel):
    scope_type: Literal["global", "sample_type", "project"]
    scope_value: str = Field(default="", max_length=100)
    retain_days: int = Field(gt=0, le=36500)
    legal_hold_days: int = Field(default=0, ge=0, le=36500)
    basis_text: str = Field(default="", max_length=500)
    change_reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _validate_scope(self):
        if self.scope_type != "global" and not self.scope_value.strip():
            raise ValueError("样品类型与项目策略必须提供 scope_value")
        return self


class LegalHoldCreate(BaseModel):
    hold_code: str | None = Field(default=None, max_length=64)
    scope_type: Literal["sample", "project", "sample_type"]
    sample_id: int | None = Field(default=None, gt=0)
    scope_value: str = Field(default="", max_length=100)
    reason: str = Field(min_length=4, max_length=500)
    hold_days: int | None = Field(default=None, gt=0, le=36500)

    @model_validator(mode="after")
    def _validate_target(self):
        if self.scope_type == "sample" and self.sample_id is None:
            raise ValueError("样品级法律保留必须提供 sample_id")
        if self.scope_type != "sample" and not self.scope_value.strip():
            raise ValueError("项目或样品类型法律保留必须提供 scope_value")
        return self


class LegalHoldRelease(BaseModel):
    reason: str = Field(min_length=2, max_length=500)


class PublicationReviewCreate(BaseModel):
    sample_id: int = Field(gt=0)
    publication_code: str = Field(min_length=2, max_length=100)
    title: str = Field(default="", max_length=300)
    expected_clear_at: str | None = Field(default=None, max_length=40)
    note: str = Field(default="", max_length=1000)


class PublicationReviewState(BaseModel):
    review_state: Literal["under_review", "published", "withdrawn"]


class ExtensionRequest(BaseModel):
    extension_code: str | None = Field(default=None, max_length=64)
    sample_id: int = Field(gt=0)
    reason: str = Field(min_length=4, max_length=500)
    extra_days: int = Field(gt=0, le=3650)


class ExtensionDecision(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=500)


class EvaluationRunRequest(BaseModel):
    evaluation_date: str | None = Field(default=None, min_length=10, max_length=10)


class DestructionPlanCreate(BaseModel):
    batch_code: str | None = Field(default=None, max_length=64)
    sample_ids: list[int] | None = Field(default=None, max_length=5000)
    note: str = Field(default="", max_length=500)
