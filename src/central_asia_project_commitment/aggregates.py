"""事件溯源聚合基类。

聚合是某条事件流的纯状态投影：version 为已应用的最后流版本（0 表示尚未创建）。
持久化与并发控制由 store + backend 负责，聚合本身不做 IO。
"""

from __future__ import annotations

from typing import Any


class Aggregate:
    stream_id: str = ""
    version: int = 0

    @classmethod
    def apply_event(cls, state: "Aggregate", event_type: str, data: dict[str, Any]) -> None:
        raise NotImplementedError

    def load(self, events: list) -> "Aggregate":
        for event in events:
            self.apply_event(self, event.type, event.data)
            self.version = event.version
            self.stream_id = event.stream
        return self
