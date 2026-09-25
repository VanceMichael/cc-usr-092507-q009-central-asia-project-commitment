"""聚合基类与仓储：领域对象只通过事件表达状态变化。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .events import EventStore


class Aggregate:
    """事件溯源聚合。

    子类实现 ``apply(event)`` 并在命令方法中用 ``record`` 产生事件；
    所有不变量在产生事件之前校验。
    """

    stream_prefix = "agg"

    def __init__(self, stream_id: str) -> None:
        self.stream_id = stream_id
        self.version = 0
        self._events: list[dict[str, Any]] = []

    @classmethod
    def stream_for(cls, entity_id: str) -> str:
        return f"{cls.stream_prefix}-{entity_id}"

    @classmethod
    def load(cls, store: EventStore, stream_id: str) -> "Aggregate | None":
        if not store.exists(stream_id):
            return None
        aggregate = cls(stream_id)
        for event in store.stream_events(stream_id):
            aggregate._apply_loaded(event)
        aggregate._events.clear()
        return aggregate

    def _apply_loaded(self, event: dict[str, Any]) -> None:
        self.version += 1
        self.apply(event)

    def record(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {"type": event_type, "payload": payload}
        self.apply(event)
        self.version += 1
        self._events.append(event)
        return event

    def apply(self, event: dict[str, Any]) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    def uncommitted(self) -> list[dict[str, Any]]:
        return list(self._events)

    def mark_committed(self) -> None:
        self._events.clear()


class Repository:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    def save(
        self,
        aggregates: Iterable[Aggregate],
        *,
        at: str,
        actor_id: str | None = None,
        request_id: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> int:
        aggs = [a for a in aggregates if a.uncommitted()]
        if not aggs:
            return self.store.all_commits()[-1].seq if self.store.all_commits() else 0
        streams = {
            a.stream_id: {"expected": a.version - len(a.uncommitted()), "events": a.uncommitted()}
            for a in aggs
        }
        metadata: dict[str, Any] = {}
        if actor_id is not None:
            metadata["actor_id"] = actor_id
        if request_id is not None:
            metadata["request_id"] = request_id
        if extra_metadata:
            metadata.update(extra_metadata)
        commit = self.store.commit(streams, at=at, metadata=metadata)
        for aggregate in aggs:
            aggregate.mark_committed()
        return commit.seq
