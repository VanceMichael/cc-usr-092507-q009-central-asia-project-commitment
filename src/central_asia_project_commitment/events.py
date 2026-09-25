"""事件存储：多流原子提交、乐观并发版本、JSONL 崩溃恢复。

一次业务命令产生的所有流事件写入同一条提交记录（单行 JSON），
因此"组合原子占用""暂留+授权"等跨流变更要么全部可见，要么全部不可见。
崩溃后重放日志即可恢复；已提交的提交不会丢失，未提交的半行被忽略。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .errors import VersionConflict


@dataclass
class Commit:
    seq: int
    at: str
    streams: dict[str, dict[str, Any]]  # stream_id -> {"expected": v, "events": [...]}
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "streams": self.streams,
            "metadata": self.metadata,
        }


class EventStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self._commits: list[Commit] = []
        self._streams: dict[str, list[dict[str, Any]]] = {}
        self._path = Path(path) if path else None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._replay()

    # ---------- 恢复 ----------
    def _replay(self) -> None:
        assert self._path is not None
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    # 崩溃留下的不完整尾行：尚未提交，忽略。
                    continue
                commit = Commit(
                    seq=raw["seq"],
                    at=raw["at"],
                    streams=raw["streams"],
                    metadata=raw.get("metadata", {}),
                )
                self._apply_commit(commit)

    def _apply_commit(self, commit: Commit) -> None:
        for stream_id, payload in commit.streams.items():
            self._streams.setdefault(stream_id, []).extend(payload["events"])
        self._commits.append(commit)

    # ---------- 读取 ----------
    def stream_version(self, stream_id: str) -> int:
        with self._lock:
            return len(self._streams.get(stream_id, ()))

    def stream_events(self, stream_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._streams.get(stream_id, ()))

    def exists(self, stream_id: str) -> bool:
        return stream_id in self._streams

    def all_commits(self) -> list[Commit]:
        with self._lock:
            return list(self._commits)

    # ---------- 提交 ----------
    def commit(
        self,
        streams: dict[str, dict[str, Any]],
        *,
        at: str,
        metadata: dict[str, Any] | None = None,
    ) -> Commit:
        """按期望版本原子提交多个流的事件。

        streams[stream_id] = {"expected": 当前版本号, "events": [...]}
        任何一个流的期望版本过期即整体拒绝（VersionConflict），
        跨时区并发争用由此收敛为单一结果。
        """
        with self._lock:
            for stream_id, payload in streams.items():
                current = len(self._streams.get(stream_id, ()))
                if payload["expected"] != current:
                    raise VersionConflict(
                        f"流 {stream_id} 版本过期：期望 {payload['expected']}，实际 {current}",
                    )
            commit = Commit(
                seq=len(self._commits) + 1,
                at=at,
                streams={
                    stream_id: {"expected": p["expected"], "events": list(p["events"])}
                    for stream_id, p in streams.items()
                },
                metadata=metadata or {},
            )
            if self._path is not None:
                self._persist(commit)
            self._apply_commit(commit)
            return commit

    def _persist(self, commit: Commit) -> None:
        assert self._path is not None
        line = json.dumps(commit.as_dict(), ensure_ascii=False, sort_keys=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    # ---------- 订阅（投影用） ----------
    def stream_ids(self, prefix: str) -> list[str]:
        with self._lock:
            return sorted(s for s in self._streams if s.startswith(prefix))

    def fold(
        self,
        prefix: str,
        build: Callable[[str], Any],
        mutate: Callable[[Any, dict[str, Any]], None],
    ) -> dict[str, Any]:
        """按前缀重放全部流，重建读模型。恢复后读模型由此重建。"""
        result: dict[str, Any] = {}
        with self._lock:
            stream_ids = [s for s in self._streams if s.startswith(prefix)]
            for stream_id in sorted(stream_ids):
                model = build(stream_id)
                for event in self._streams[stream_id]:
                    mutate(model, event)
                result[stream_id] = model
        return result
