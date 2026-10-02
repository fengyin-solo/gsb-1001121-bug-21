"""鉴权接口：登录换票、当前身份、注销。令牌即鉴权缓存键。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.schemas import LoginPayload, LoginResult
from app.services.handover import handover_service
from app.services.identity import Identity, auth_service, require_identity

router = APIRouter(prefix="/api/auth", tags=["班组鉴权"])


@router.post("/login", response_model=LoginResult)
def login(payload: LoginPayload) -> LoginResult:
    """按工号登录（演示环境免密），返回的令牌放进 Authorization: Bearer。"""
    user, token, expires_at = auth_service.login(payload.user_code.strip())
    shift = handover_service.current_shift(user["班组"]) if user.get("班组") else None
    return LoginResult(
        token=token,
        user={"工号": user["工号"], "姓名": user["姓名"], "班组": user["班组"], "角色": user["角色"]},
        shift=shift,
        expires_at=expires_at.isoformat(timespec="seconds"),
    )


@router.get("/me")
def me(identity: Identity = Depends(require_identity)) -> dict[str, object]:
    """解析当前令牌身份与所在班组的当前班次。"""
    return {
        "identity": identity.as_dict(),
        "current_shift": handover_service.current_shift(identity.crew),
    }


@router.post("/logout")
def logout(identity: Identity = Depends(require_identity)) -> dict[str, bool]:
    """主动注销当前令牌。"""
    return {"ok": auth_service.logout(identity.session_token)}
