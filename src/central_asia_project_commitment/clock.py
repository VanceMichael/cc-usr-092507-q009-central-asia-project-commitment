"""时间工具：统一 UTC 存储，按 IANA 时区呈现截止时间与审批时刻。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

UTC = timezone.utc


class Clock(Protocol):
    def current(self) -> datetime: ...


class SystemClock:
    """生产时钟，每次读取真实当前时刻。"""

    def current(self) -> datetime:
        return datetime.now(tz=UTC)


class FixedClock:
    """测试/回放时钟，时刻可手动推进。"""

    def __init__(self, moment: datetime | None = None):
        self._moment = moment.astimezone(UTC) if moment else datetime.now(tz=UTC)

    def current(self) -> datetime:
        return self._moment

    def advance(self, delta: timedelta) -> None:
        self._moment += delta

    def set(self, moment: datetime) -> None:
        self._moment = moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)


def after(clock: Clock, delta: timedelta) -> datetime:
    return clock.current() + delta


def parse(value: str) -> datetime:
    """解析 ISO-8601；无时区按 UTC 处理。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def format_in(dt: datetime, tz_name: str) -> str:
    """按参与者所在时区呈现同一时刻。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ZoneInfo(tz_name)).isoformat()


def business_day(dt: datetime, tz_name: str) -> date:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ZoneInfo(tz_name)).date()
