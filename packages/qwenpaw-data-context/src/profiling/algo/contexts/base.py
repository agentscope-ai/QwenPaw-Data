"""Context 基类：一个泛型 + 两种加载策略。

------------------------------------------------------------------------
Context 不知道 snapshot 从哪来
------------------------------------------------------------------------
它只拿到「一个能产出 snapshot 的协程」。查远程库、跑转换、读另一个
context，对它没有区别 —— ``QueryLogContext`` 的 fetch 就是「读上游
RawQueryLogContext 再解析」。

------------------------------------------------------------------------
两种策略分的是**优先级**，不是来源类型
------------------------------------------------------------------------
``SourceContext``   本地优先 —— 远程贵且稳定，本地命中就用
``ArtifactContext`` 权威优先 —— 已确认事实必须压过本地快照

第二条不是洁癖：若本地优先，用户在界面上改过 domain 归属之后，下游会读到
上次跑的本地 parquet，**修改不生效而且不报错**。那正是旧版
「用户确认对下游不可见」的缺陷形态。
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import (
    Any,
    Awaitable,
    Callable,
    ClassVar,
    Generic,
    Mapping,
    Sequence,
    TypeVar,
)

import pandas as pd

from ..snapshots import Snapshot
from ..stores import LocalStore, ReadableLocalStore, first_readable
from ..tasks import ProfileTask

logger = logging.getLogger(__name__)

S = TypeVar("S", bound=Snapshot)

#: 落盘的 manifest 名。记录 key 清单与指纹 —— 回读**不从 key 反推文件名**，
#: store 的 ``_safe()`` 会让中文表名撞车。
MANIFEST = "snapshot"


class Lifecycle(str, Enum):
    """产出的生命周期。

    **不是** ``profiling/stages.py`` 那六个探查环节，也不是 ``ROW_*`` 行状态
    —— 行状态（PENDING / CONFIRMED / SKIPPED）与来源（ALGO / HUMAN）是
    staged 快照**里的列**，不是快照的种类。

    这两个之所以要拆成两份快照，是因为 promote 是唯一的**双重边界**：
    跨存储（staging 表 → 正式表）**且**跨可变性（promote 后算法不能改）。
    """

    #: staging 表：算法产出 + 人在编辑。可被下一轮覆盖，改动经 EmitStore 回传核验
    STAGED = "staged"
    #: 正式表：人已确认并 promote。算法只读消费
    CONFIRMED = "confirmed"


class SnapshotContext(ABC, Generic[S]):
    """管理一份 snapshot 的本地生命周期。"""

    name: ClassVar[str] = ""
    #: 本 context 能为哪些任务提供证据。**这就是依赖图的唯一定义点。**
    SUPPORTED_TASKS: ClassVar[frozenset[ProfileTask]] = frozenset()

    def __init__(
        self,
        stores: Sequence[LocalStore] = (),
        *,
        fingerprint: Mapping[str, Any] | None = None,
    ) -> None:
        self._stores = tuple(stores)
        self._cache: ReadableLocalStore | None = first_readable(self._stores)
        self._current: S | None = None
        self._fingerprint = dict(fingerprint or {})

    # -- 子类实现 --------------------------------------------------------

    @abstractmethod
    def empty(self) -> S:
        """空快照。加载失败与首次运行的统一返回值。"""

    @abstractmethod
    def rebuild(self, frames: Mapping[str, pd.DataFrame]) -> S:
        """由 frames 重建。通常直接转调 ``SomeSnapshot.from_frames``。"""

    @abstractmethod
    async def load(self, *, refresh: bool = False) -> S: ...

    @abstractmethod
    def evidence_for_task(self, task: ProfileTask) -> Any | None:
        """把自己的 snapshot 投影成该任务能用的形状。不支持则 None。"""

    def fingerprint(self) -> dict[str, Any]:
        """本地缓存的失效判据。空 dict = 永不失效。

        由装配层**注入**而不是子类生成 —— 判据与提供 fetch 逻辑的层同源
        （各来源类的 ``fingerprint(config)``，与 ``create`` 并排同址），
        context 按设计不知道自己的来源读了哪些配置。
        """
        return dict(self._fingerprint)

    # -- 通用 ------------------------------------------------------------

    def supports(self, task: ProfileTask) -> bool:
        return task in self.SUPPORTED_TASKS

    def current(self) -> S:
        return self._current if self._current is not None else self.empty()

    def is_loaded(self) -> bool:
        return self._current is not None

    async def save(self, snapshot: S, *, emit: bool = True) -> None:
        """写 store 并成为 current。

        单个 store 失败不拖垮整体 —— emit 吐不出去，不该让已算好的结果丢失。

        ``emit=False`` 跳过 ``kind == "emit"`` 的 store。**从 facts 刷新本地
        缓存时必须用它** —— 那份内容本来就是后端给的，再 emit 回去就是回环。
        """
        self._current = snapshot
        targets = (
            self._stores if emit else tuple(s for s in self._stores if s.kind != "emit")
        )
        if not targets:
            return
        frames = dict(snapshot.to_frames())
        manifest = {
            "context": self.name,
            "keys": sorted(frames),
            "fingerprint": self.fingerprint(),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "rows": {k: int(len(v)) for k, v in frames.items()},
        }
        await asyncio.gather(
            *(self._write_one(store, frames, manifest) for store in targets),
            return_exceptions=False,
        )
        logger.info(
            "%s: 已落盘 %s 个 frame → %s",
            self.name,
            len(frames),
            [s.kind for s in targets],
        )

    async def _write_one(
        self,
        store: LocalStore,
        frames: Mapping[str, pd.DataFrame],
        manifest: Mapping[str, Any],
    ) -> None:
        try:
            for key, frame in frames.items():
                await store.write(key, frame)
            await store.write_meta(MANIFEST, manifest)
        except Exception as exc:
            logger.warning("%s: store %s 写入失败: %s", self.name, store.kind, exc)

    async def read_local(self) -> S | None:
        """从缓存 store 回读。指纹不匹配或 manifest 缺失时返回 None。"""
        if self._cache is None:
            return None
        manifest = await self._cache.read_meta(MANIFEST)
        if not manifest:
            return None

        expected = self.fingerprint()
        if expected and manifest.get("fingerprint") != expected:
            logger.info("%s: 指纹不匹配，本地缓存作废", self.name)
            return None

        frames: dict[str, pd.DataFrame] = {}
        for key in manifest.get("keys") or ():
            frame = await self._cache.read(key)
            if frame is not None:
                frames[key] = frame
        if not frames:
            return None
        return self.rebuild(frames)


# 这里曾有一个 ``write_local()``（只刷缓存 store）。它存在的唯一理由是
# 「从后端读来的内容不能再 emit 回去」，现在由 ``save(..., emit=False)``
# 表达 —— 后者还能把 Excel 等非缓存 store 一并刷新，语义更准。


# ===========================================================================
# 两种加载策略
# ===========================================================================


class SourceContext(SnapshotContext[S]):
    """外部来源：**本地优先**。远程贵且稳定，本地命中就用。"""

    def __init__(
        self,
        stores: Sequence[LocalStore] = (),
        *,
        fetch: Callable[[], Awaitable[S]] | None = None,
        fingerprint: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(stores, fingerprint=fingerprint)
        self._fetch = fetch

    async def load(self, *, refresh: bool = False) -> S:
        if self._current is not None and not refresh:
            return self._current

        if not refresh:
            cached = await self.read_local()
            if cached is not None:
                self._current = cached
                logger.info("%s: 命中本地缓存", self.name)
                return cached

        if self._fetch is None:
            logger.info("%s: 无 fetch，返回空快照（正常路径）", self.name)
            self._current = self.empty()
            return self._current

        snapshot = await self._fetch()
        await self.save(snapshot)
        return snapshot


class ArtifactContext(SnapshotContext[S]):
    """任务产物：**事实优先，本地兜底**。

    每种产物有两个实例，只差两件事 —— 读 ``ProbeFactsInput`` 的哪一侧，
    以及 store 列表里挂不挂 ``EmitStore``。其余完全一致，**confirmed 侧
    同样可以 save / load**，不做只读保护（后端自行保护）。

    ``fact`` 是装配层预绑好的读取器（已经选定了 lifecycle 那一侧），
    算法层因此不认识 ``ProbeFactsInput`` 的形状，也碰不到租户与权限。
    为 None 时退化成纯本地 —— 离线跑批路径。
    """

    def __init__(
        self,
        stores: Sequence[LocalStore] = (),
        *,
        name: str | None = None,
        lifecycle: Lifecycle = Lifecycle.STAGED,
        fact: Callable[[], Awaitable[S | None]] | None = None,
        # 接口预留：artifact 侧的失效判据尚未实现，装配层暂不注入
        fingerprint: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(stores, fingerprint=fingerprint)
        # name 从 ClassVar 降为实例属性：同一个类要出两个实例（staged /
        # confirmed），名字必须能分开
        if name is not None:
            self.name = name
        self.lifecycle = lifecycle
        self._fact = fact

    async def load(self, *, refresh: bool = False) -> S:
        """事实优先 → 本地缓存 → 空。

        **事实压过本地**：staging 里的行可能被人改过，必须压过算法上一轮
        的落盘；confirmed 侧更是 truth。刷新本地缓存时 ``emit=False``——
        那份内容本来就是后端给的，再吐回去就是回环。
        """
        if self._current is not None and not refresh:
            return self._current

        if self._fact is not None:
            try:
                fact = await self._fact()
            except Exception as exc:
                logger.warning("%s: 读事实失败，回落本地: %s", self.name, exc)
                fact = None
            if fact is not None and not fact.is_empty():
                logger.info(
                    "%s: 采用 %s 事实（压过本地快照）", self.name, self.lifecycle.value
                )
                await self.save(fact, emit=False)
                return fact

        cached = await self.read_local()
        self._current = cached if cached is not None else self.empty()
        return self._current


__all__ = [
    "MANIFEST",
    "ArtifactContext",
    "Lifecycle",
    "SnapshotContext",
    "SourceContext",
]
