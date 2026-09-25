"""时钟：系统内部一律使用 UTC 绝对时间，时区只作为展示与记录信息。

跨时区审批的先后由 UTC 时刻决定，避免不同本地时钟产生两个结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Moment:
    """UTC 绝对时刻 + 操作人所在 IANA 时区（仅记录用）。"""

    at: datetime
    zone: str = "UTC"

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            raise ValueError("时刻必须带时区信息")

    @property
    def utc(self) -> datetime:
        return self.at.astimezone(timezone.utc)

    def local_text(self) -> str:
        return self.at.astimezone(ZoneInfo(self.zone)).isoformat()


class Clock:
    """可替换时钟；生产用系统时钟，测试用固定时钟。"""

    def __init__(self, zone: str = "UTC") -> None:
        self._zone = zone

    def now(self, zone: str | None = None) -> Moment:
        return Moment(datetime.now(timezone.utc), zone or self._zone)

    def at(self, when: datetime, zone: str | None = None) -> Moment:
        if when.tzinfo is None:
            when = when.replace(tzinfo=ZoneInfo(zone or self._zone))
        return Moment(when, zone or self._zone)
