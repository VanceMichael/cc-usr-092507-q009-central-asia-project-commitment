"""领域错误：全部携带稳定的机器可读代码，供跨时区接口返回单一结果。"""

from __future__ import annotations


class DomainError(Exception):
    """所有领域规则违反的基类。"""

    code = "domain_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class NotFound(DomainError):
    code = "not_found"


class VersionConflict(DomainError):
    """乐观并发冲突：同一流被他人先行提交，调用方必须重读再决定。"""

    code = "version_conflict"


class RuleViolation(DomainError):
    code = "rule_violation"


class AuthorizationError(DomainError):
    code = "forbidden"


class IdempotencyReplay(DomainError):
    """同一业务请求重放：不产生第二份承诺，返回首次结果。"""

    code = "idempotency_replay"

    def __init__(self, message: str, *, result: dict | None = None) -> None:
        super().__init__(message)
        self.result = result or {}
