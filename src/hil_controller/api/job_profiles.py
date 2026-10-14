"""Authenticated public recipe and credential guidance (never stored secrets)."""

from typing import Any

from fastapi import APIRouter

from hil_controller.api.jobs import Auth
from hil_controller.job_profiles import AgentJob, job_guide

router = APIRouter(prefix="/v1/job-profiles", tags=["jobs"])


@router.get("")
async def get_job_profiles(_auth: Auth) -> dict[str, Any]:
    return {**job_guide(), "request_schema": AgentJob.model_json_schema()}
