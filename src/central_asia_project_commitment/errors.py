"""领域错误类型。"""

from __future__ import annotations


class DomainError(Exception):
    """所有领域规则冲突的基类。"""


class NotFound(DomainError):
    """聚合或实体不存在。"""


class VersionConflict(DomainError):
    """依据的版本已过期，跨时区并发争用必须重新读取后再提交。"""


class StateConflict(DomainError):
    """当前状态不允许该操作（如重复确认、承诺已终止）。"""


class PolicyViolation(DomainError):
    """违反业务策略：双方确认、发起/复核分离、授权范围等。"""


class ResourceConflict(DomainError):
    """组合资源中的具体资源池容量不足或被其他承诺占用。"""

    def __init__(self, conflicts: list[dict]):
        self.conflicts = conflicts
        super().__init__(f"资源冲突：{conflicts}")


class IdempotencyReplayed(DomainError):
    """同一业务请求键重放，但载荷与首次不一致。"""
