"""开工体检接口。

前端首页只调 `GET /api/ready` 一次，就能决定显示"可以开工"还是"还差两件事"。
每个"还差"的项都带一个 action，点下去直接修——不懂电脑的人不需要知道
背后是启动一个 exe 还是拉一个浏览器。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import ready

router = APIRouter(prefix="/api/ready", tags=["ready"])


class StartRequest(BaseModel):
    target: str = Field(description="xiaohongshu / douyin")


class ToggleRequest(BaseModel):
    platform: str = Field(description="xiaohongshu / douyin")
    enabled: bool = True


@router.get("")
def check_ready(refresh: bool = False):
    return ready.check(force=refresh)


@router.post("/start")
def start_service(payload: StartRequest):
    if payload.target == "xiaohongshu":
        result = ready.start_xiaohongshu()
    elif payload.target == "douyin":
        result = ready.start_douyin()
    else:
        raise HTTPException(400, "不认识这个目标")
    # 起完之后顺带刷新一次体检，前端不用再请求一轮
    return {**result, "ready": ready.check(force=True)}


@router.post("/toggle")
def toggle(payload: ToggleRequest):
    result = ready.toggle_platform(payload.platform, payload.enabled)
    if not result["ok"]:
        raise HTTPException(400, result["message"])
    return {**result, "ready": ready.check(force=True)}


@router.get("/diagnose")
def diagnose():
    return ready.diagnose()
