"""只追加事件存储与幂等键表。

- 每个流（主体/意向/承诺/尽调案件/资源账本/项目）独立递增版本；
- 写入带期望版本，跨进程文件锁内校验，争用只产生一个结果；
- 业务请求键（request_key）记录首次提交的载荷指纹与结果引用，
  重放返回首次结果，绝不产生第二份承诺。
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from collections.abc import Callable

from .errors import IdempotencyReplayed, VersionConflict


@dataclass(frozen=True)
class Event:
    seq: int
    stream: str
    version: int
    type: str
    data: dict[str, Any]
    ts: str
    request_key: str | None = None

    def to_json(self) -> dict[str, Any]:
        value = {
            "seq": self.seq,
            "stream": self.stream,
            "version": self.version,
            "type": self.type,
            "data": self.data,
            "ts": self.ts,
        }
        if self.request_key is not None:
            value["request_key"] = self.request_key
        return value

    @staticmethod
    def from_json(value: dict[str, Any]) -> "Event":
        return Event(
            seq=value["seq"],
            stream=value["stream"],
            version=value["version"],
            type=value["type"],
            data=value["data"],
            ts=value["ts"],
            request_key=value.get("request_key"),
        )


def fingerprint(payload: Any) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class EventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.keys_path = self.path.with_suffix(".requests.json")
        if not self.keys_path.exists():
            self.keys_path.write_text("{}", encoding="utf-8")

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self.lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load_lines(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def read_all(self) -> list[Event]:
        return [Event.from_json(line) for line in self._load_lines()]

    def read_stream(self, stream: str) -> list[Event]:
        return [event for event in self.read_all() if event.stream == stream]

    def stream_version(self, stream: str) -> int:
        version = 0
        for event in self.read_stream(stream):
            version = event.version
        return version

    @staticmethod
    def exists_stream(stream: str) -> str:
        return stream

    def append_many(
        self,
        writes: list[tuple[str, int, list[tuple[str, dict[str, Any]]]]],
        ts: str,
        request_key: str | None = None,
    ) -> list[Event]:
        """一批写多个流（如承诺事件 + 资源账本事件），同一把锁内原子提交。"""
        with self._locked():
            return self._append_many_locked(writes, ts, request_key)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """显式临界区：在其中重新读流做最后校验，保证争用单一结果。"""
        with self._locked():
            yield

    def commit_unit(
        self,
        decider,
        ts: str,
        request_key: str | None = None,
        payload_fingerprint: str = "",
    ):
        """锁内决策 + 原子提交。

        decider 在持锁期间执行，应通过 store 的只读方法读取**最新**状态后
        返回 ``(writes, result_ref, apply_pairs)``；抛异常则什么都不写。
        apply_pairs 为 [(state, events)]，供调用方推进本地内存状态。

        这保证跨时区并发命令依据同一最新版本给出单一结果。
        """
        with self._locked():
            if request_key is not None:
                keys = self._load_keys()
                record = keys.get(request_key)
                if record is not None:
                    if record["fingerprint"] != payload_fingerprint:
                        raise IdempotencyReplayed(
                            f"请求键 {request_key} 已用于不同载荷，禁止重复承诺"
                        )
                    return True, dict(record["result_ref"]), []
            writes, ref, apply_pairs = decider()
            self._append_many_locked(writes, ts, request_key)
            if request_key is not None:
                keys = self._load_keys()
                streams = sorted({stream for stream, _v, _e in writes})
                keys[request_key] = {
                    "fingerprint": payload_fingerprint,
                    "streams": streams,
                    "result_ref": dict(ref),
                    "ts": ts,
                }
                self._write_keys(keys)
            return False, dict(ref), apply_pairs

    def guarded_append_many(
        self,
        writes: list[tuple[str, int, list[tuple[str, dict[str, Any]]]]],
        ts: str,
        verifier: Callable[[], None],
        request_key: str | None = None,
        payload_fingerprint: str = "",
        result_ref: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """锁内先跑 verifier（可重读全部相关流），通过才原子追加。

        verifier 抛错则整批不写。带 request_key 时重放直接返回首次结果。
        """
        with self._locked():
            if request_key is not None:
                keys = self._load_keys()
                record = keys.get(request_key)
                if record is not None:
                    if record["fingerprint"] != payload_fingerprint:
                        raise IdempotencyReplayed(
                            f"请求键 {request_key} 已用于不同载荷，禁止重复承诺"
                        )
                    return {**record["result_ref"], "replayed": True}
            verifier()
            appended = self._append_many_locked(writes, ts, request_key)
            ref = dict(result_ref or {})
            if request_key is not None:
                keys = self._load_keys()
                keys[request_key] = {
                    "fingerprint": payload_fingerprint,
                    "streams": sorted({event.stream for event in appended}),
                    "seqs": [event.seq for event in appended],
                    "result_ref": ref,
                    "ts": ts,
                }
                self._write_keys(keys)
            return {**ref, "replayed": False}

    def _append_many_locked(
        self,
        writes: list[tuple[str, int, list[tuple[str, dict[str, Any]]]]],
        ts: str,
        request_key: str | None,
    ) -> list[Event]:
        for stream, expected_version, _events in writes:
            current = self.stream_version(stream)
            if current != expected_version:
                raise VersionConflict(
                    f"流 {stream} 版本 {expected_version} 已过期，当前为 {current}"
                )
        lines = self._load_lines()
        next_seq = max((line["seq"] for line in lines), default=0) + 1
        appended: list[Event] = []
        for stream, expected_version, events in writes:
            current = expected_version
            for event_type, data in events:
                current += 1
                event = Event(
                    seq=next_seq,
                    stream=stream,
                    version=current,
                    type=event_type,
                    data=data,
                    ts=ts,
                    request_key=request_key,
                )
                next_seq += 1
                appended.append(event)
        with self.path.open("a", encoding="utf-8") as handle:
            for event in appended:
                handle.write(json.dumps(event.to_json(), ensure_ascii=False) + "\n")
        return appended

    def _load_keys(self) -> dict[str, Any]:
        return json.loads(self.keys_path.read_text(encoding="utf-8") or "{}")

    def _write_keys(self, keys: dict[str, Any]) -> None:
        tmp = self.keys_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(keys, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.keys_path)

    def replay_or_append(
        self,
        request_key: str,
        payload_fingerprint: str,
        writes: list[tuple[str, int, list[tuple[str, dict[str, Any]]]]],
        result_ref: dict[str, Any],
        ts: str,
    ) -> tuple[bool, dict[str, Any]]:
        """幂等追加。

        返回 (是否重放, 结果引用)。重放且指纹一致时返回首次结果引用；
        同一键不同载荷直接拒绝；新请求在锁内完成全部流的版本校验与追加。
        """
        with self._locked():
            keys = self._load_keys()
            record = keys.get(request_key)
            if record is not None:
                if record["fingerprint"] != payload_fingerprint:
                    raise IdempotencyReplayed(
                        f"请求键 {request_key} 已用于不同载荷，禁止重复承诺"
                    )
                return True, record["result_ref"]
            appended = self._append_many_locked(writes, ts, request_key)
            keys[request_key] = {
                "fingerprint": payload_fingerprint,
                "streams": sorted({event.stream for event in appended}),
                "seqs": [event.seq for event in appended],
                "result_ref": result_ref,
                "ts": ts,
            }
            self._write_keys(keys)
            return False, result_ref

    def request_record(self, request_key: str) -> dict[str, Any] | None:
        return self._load_keys().get(request_key)
