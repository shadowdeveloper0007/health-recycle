import asyncio
import re

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator

from scrape import (
    DashboardInfoResult,
    RewardItem,
    ScrapeResult,
    scrape_dashboard_info,
    scrape_login_time,
)

app = FastAPI(title="3T Login Time API", version="1.0.0")
_scrape_lock = asyncio.Lock()

EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+-]+@cssoftsolutions\.com$", re.IGNORECASE)


class LoginTimeRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        email = value.strip()
        if not EMAIL_PATTERN.match(email):
            raise ValueError("Please check your email!")
        return email

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Please Enter Valid Password")
        return value


class LoginTimeResponse(BaseModel):
    success: bool
    found: bool
    login_time: str | None
    date: str
    message: str
    field: str | None = None


def _to_response(result: ScrapeResult) -> LoginTimeResponse:
    return LoginTimeResponse(
        success=not result.error and not result.validation_error,
        found=result.found,
        login_time=result.login_time,
        date=result.date,
        message=result.message,
        field=result.field,
    )


@app.post("/api/login-time", response_model=LoginTimeResponse)
async def get_login_time(body: LoginTimeRequest) -> LoginTimeResponse:
    """Scrape today's biometric login time from 3T for the given user."""
    async with _scrape_lock:
        result = await scrape_login_time(body.email, body.password, True)
    response = _to_response(result)

    if result.validation_error:
        raise HTTPException(status_code=401, detail=response.model_dump())

    if result.error:
        raise HTTPException(status_code=500, detail=response.model_dump())

    if not result.found:
        raise HTTPException(status_code=404, detail=response.model_dump())

    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ── Dashboard Info models & endpoint ───────────────────────────────────────────


class RewardItemResponse(BaseModel):
    name: str
    icon_url: str
    count: int


class DashboardInfoResponse(BaseModel):
    success: bool
    name: str | None
    designation: str | None
    employee_id: str | None
    date_of_joining: str | None
    total_experience: str | None
    status: str | None
    manager: str | None
    profile_url: str | None
    rewards: list[RewardItemResponse]
    project_productivity: str | None
    message: str


def _to_dashboard_response(result: DashboardInfoResult) -> DashboardInfoResponse:
    return DashboardInfoResponse(
        success=result.success,
        name=result.name,
        designation=result.designation,
        employee_id=result.employee_id,
        date_of_joining=result.date_of_joining,
        total_experience=result.total_experience,
        status=result.status,
        manager=result.manager,
        profile_url=result.profile_url,
        rewards=[
            RewardItemResponse(name=r.name, icon_url=r.icon_url, count=r.count)
            for r in result.rewards
        ],
        project_productivity=result.project_productivity,
        message=result.message,
    )


@app.post("/api/dashboard-info", response_model=DashboardInfoResponse)
async def get_dashboard_info(body: LoginTimeRequest) -> DashboardInfoResponse:
    """Scrape dashboard info: name, designation, profile URL, rewards, and project productivity."""
    async with _scrape_lock:
        result = await scrape_dashboard_info(body.email, body.password, True)
    response = _to_dashboard_response(result)

    if result.validation_error:
        raise HTTPException(status_code=401, detail=response.model_dump())

    if result.error:
        raise HTTPException(status_code=500, detail=response.model_dump())

    return response
