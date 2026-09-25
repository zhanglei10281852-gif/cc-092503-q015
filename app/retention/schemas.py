from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class PolicyCreate(BaseModel):
    policy_code: str = Field(min_length=2, max_length=64)
    sample_type: str = Field(min_length=1, max_length=100)
    project_code: str | None = Field(default=None, min_length=2, max_length=64)
    retention_months: int = Field(gt=0, le=1200)
    description: str = Field(default="", max_length=500)


class PolicyVersionCreate(BaseModel):
    retention_months: int = Field(gt=0, le=1200)
    description: str = Field(default="", max_length=500)


class RetentionRunRequest(BaseModel):
    run_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    batch_size: int = Field(default=200, gt=0, le=5000)
    max_batches: int | None = Field(default=None, gt=0, le=1000)
    lease_seconds: int = Field(default=300, ge=30, le=3600)


class ExtensionCreate(BaseModel):
    sample_id: int = Field(gt=0)
    extend_until: str = Field(min_length=10, max_length=40)
    reason: str = Field(min_length=4, max_length=500)


class LegalHoldCreate(BaseModel):
    hold_code: str | None = Field(default=None, max_length=64)
    sample_id: int | None = Field(default=None, gt=0)
    project_code: str | None = Field(default=None, min_length=2, max_length=64)
    reason: str = Field(min_length=4, max_length=500)

    @model_validator(mode="after")
    def ensure_target(self):
        if not self.sample_id and not self.project_code:
            raise ValueError("sample_id 与 project_code 至少填写一个")
        return self


class LegalHoldRelease(BaseModel):
    note: str = Field(default="", max_length=500)


class PaperRefCreate(BaseModel):
    reference_code: str | None = Field(default=None, max_length=64)
    sample_id: int = Field(gt=0)
    publication: str = Field(min_length=2, max_length=200)
    note: str = Field(default="", max_length=500)


class ReportGenerate(BaseModel):
    run_id: int | None = Field(default=None, gt=0)


class DestructionPlanCreate(BaseModel):
    report_id: int = Field(gt=0)
    sample_ids: list[int] | None = Field(default=None, max_length=500)


class DestructionItemExecute(BaseModel):
    method: str = Field(min_length=2, max_length=200)
    witness_one: int = Field(gt=0)
    witness_two: int = Field(gt=0)
