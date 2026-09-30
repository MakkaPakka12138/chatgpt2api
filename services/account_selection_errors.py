"""Public account-selection errors and credential-free validation diagnostics."""
from __future__ import annotations

import re
from typing import Any


class AccountSelectionError(RuntimeError):
    def __init__(self, message: str, *, status_code: int, code: str,
                 account_selection: dict[str, Any] | None = None, account_email: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.error_type = "insufficient_quota" if code == "insufficient_quota" else "server_error"
        self.account_selection = account_selection
        self.account_email = account_email


class NoAvailableImageAccountError(AccountSelectionError):
    def __init__(self, *, references: bool = False, account_filter: str = "") -> None:
        scope = f"符合 {account_filter} 条件的" if account_filter else "可用的"
        message = f"没有{scope}生图账号，请检查账号状态和图片额度。"
        if references:
            message += "使用新参考图时，账号上传额度须至少剩余 20 次，并足够上传本次参考图。"
        super().__init__(message, status_code=429, code="insufficient_quota")


def validation_diagnostic(exc: Exception, access_token: str) -> dict[str, Any]:
    # Errors can contain a proxy URL or echoed authorization, so scrub before persisting.
    error = str(exc) or type(exc).__name__
    if access_token:
        error = error.replace(access_token, "<redacted>")
    error = re.sub(r"eyJ[A-Za-z0-9_.-]{15,}", "<redacted>", error)
    error = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer <redacted>", error)
    error = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "<redacted>", error)
    error = re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@]+@", r"\1<redacted>@", error)
    error = re.sub(
        r"(?i)([?&](?:access_token|refresh_token|api[_-]?key|password|token)=)[^\s&]+",
        r"\1<redacted>", error,
    )
    lower = error.lower()
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        match = re.search(r"(?i)\b(?:HTTP(?:/\d(?:\.\d)?)?|status(?:_code)?)\s*[:=]?\s*(\d{3})\b", error)
        status = int(match[1]) if match else None
    if status == 401 or "token invalidated" in lower or "token_invalidated" in lower:
        kind, reason = "authentication", "账号登录已失效"
    elif status == 403:
        kind, reason = "http_error", "上游拒绝访问（HTTP 403）"
    elif status == 429:
        kind, reason = "rate_limit", "上游请求过于频繁（HTTP 429）"
    elif status:
        kind, reason = "http_error", f"上游返回 HTTP {status}"
    elif any(text in lower for text in ("curl: (28)", "timed out", "timeout")):
        kind, reason = "timeout", "连接上游超时"
    elif any(text in lower for text in ("curl: (35)", "curl: (60)", "ssl", "tls", "certificate")):
        kind, reason = "tls", "安全连接失败"
    elif any(text in lower for text in ("proxy", "curl: (5)", "curl: (7)", "curl: (97)")):
        kind, reason = "proxy_connection", "代理连接失败"
    elif any(text in lower for text in ("connection", "resolve host", "curl: (6)")):
        kind, reason = "connection", "连接上游失败"
    else:
        kind, reason = "validation", "上游账号验证出错"
    return {"error_type": type(exc).__name__, "error": error[:2000], "reason": reason,
            "category": kind, **({"upstream_status": status} if status else {})}


def validation_selection_error(attempts: int, errors: list[dict[str, Any]]) -> AccountSelectionError:
    last = errors[-1]
    return AccountSelectionError(
        f"生图前账号验证失败：已检查 {attempts} 个账号，其中 {len(errors)} 个验证出错；"
        f"最后原因：{last['reason']}。请稍后重试，管理员可在日志详情查看具体原因。",
        status_code=502, code="account_validation_failed",
        account_selection={"attempted_accounts": attempts, "validation_failed": len(errors), "errors": errors},
        account_email=str(last.get("account_email") or ""),
    )
