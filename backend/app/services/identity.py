"""身份与鉴权缓存：班组人员、登录会话（令牌缓存）都收在这里。

会话里缓存的是登录那一刻的身份快照（人员、班组、角色），所以鉴权解析走的是
「鉴权缓存」而不是实时反查人员表；交接签字后旧班次签发的会话会被整体失效，
旧身份再拿旧令牌派单会在这里被直接拦截。
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status

from app.store import store

USER_TABLE = "users"
SESSION_TABLE = "auth_sessions"

SESSION_TTL = timedelta(hours=8)


@dataclass(frozen=True)
class Identity:
    """一次鉴权解析出来的调用方身份。"""

    session_token: str
    user_code: str
    name: str
    crew: str
    role: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_code": self.user_code,
            "name": self.name,
            "crew": self.crew,
            "role": self.role,
        }


class AuthService:
    def _utcnow(self) -> datetime:
        return datetime.utcnow()

    # ---- 人员 ----------------------------------------------------------
    def find_user(self, user_code: str) -> dict[str, Any] | None:
        for user in store.rows(USER_TABLE):
            if user.get("工号") == user_code and user.get("active", True):
                return user
        return None

    # ---- 登录 / 鉴权缓存 -------------------------------------------------
    def login(self, user_code: str) -> tuple[dict[str, Any], str, datetime]:
        """签发令牌并写入鉴权缓存；缓存内容即登录时身份快照。"""
        user = self.find_user(user_code)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="工号不存在或已停用")
        token = secrets.token_hex(24)
        expires_at = self._utcnow() + SESSION_TTL
        session = {
            "id": store.next_id(SESSION_TABLE),
            "token": token,
            "user_code": user["工号"],
            "name": user["姓名"],
            "crew": user["班组"],
            "role": user["角色"],
            "active": True,
            "created_at": self._utcnow().isoformat(timespec="seconds"),
            "expires_at": expires_at.isoformat(timespec="seconds"),
            "last_used_at": self._utcnow().isoformat(timespec="seconds"),
            "revoked_reason": None,
        }
        store.rows(SESSION_TABLE).append(session)
        return user, token, expires_at

    def logout(self, token: str) -> bool:
        session = self._find_session(token)
        if session is None:
            return False
        session["active"] = False
        session["revoked_reason"] = "主动注销"
        return True

    def _find_session(self, token: str) -> dict[str, Any] | None:
        for session in store.rows(SESSION_TABLE):
            if session.get("token") == token:
                return session
        return None

    def sessions_of_user(self, user_code: str, *, active_only: bool = True) -> list[dict[str, Any]]:
        rows = store.rows(SESSION_TABLE)
        return [
            row for row in rows
            if row.get("user_code") == user_code and (not active_only or row.get("active"))
        ]

    def revoke_sessions(
        self,
        *,
        crew: str | None = None,
        user_codes: list[str] | None = None,
        reason: str,
    ) -> int:
        """失效会话缓存。交接时旧班次下班的人（班长/旧司机）的令牌全部作废。

        ``user_codes`` 是工号白名单；不传则失效该班组全部会话。
        """
        targets = {str(code) for code in user_codes} if user_codes is not None else None
        count = 0
        now = self._utcnow().isoformat(timespec="seconds")
        for session in store.rows(SESSION_TABLE):
            if not session.get("active"):
                continue
            if crew is not None and session.get("crew") != crew:
                continue
            if targets is not None and str(session.get("user_code")) not in targets:
                continue
            session["active"] = False
            session["revoked_reason"] = reason
            session["revoked_at"] = now
            count += 1
        return count

    def resolve(self, token: str | None) -> Identity:
        """按令牌读鉴权缓存：过期、作废、旧班次残留一律拒绝。"""
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="未提供身份令牌，请先登录后再操作",
            )
        session = self._find_session(token)
        now = self._utcnow()
        if session is None or not session.get("active"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="身份令牌已失效：班组交接后旧班次令牌会被回收，请重新登录",
            )
        if datetime.fromisoformat(session["expires_at"]) <= now:
            session["active"] = False
            session["revoked_reason"] = "令牌过期"
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="身份令牌已过期，请重新登录")
        session["last_used_at"] = now.isoformat(timespec="seconds")
        return Identity(
            session_token=token,
            user_code=session["user_code"],
            name=session["name"],
            crew=session["crew"],
            role=session["role"],
        )


auth_service = AuthService()


def require_identity(request: Request) -> Identity:
    """FastAPI 依赖：从 Authorization: Bearer <token> 解析当前身份。"""
    header = request.headers.get("Authorization") or ""
    token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else None
    return auth_service.resolve(token)
