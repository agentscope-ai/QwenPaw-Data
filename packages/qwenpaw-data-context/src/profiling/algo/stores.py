"""本地化存储。一个 context 可挂多个。

------------------------------------------------------------------------
两类载荷，写进类型
------------------------------------------------------------------------
算法层的产出只有两种形状，因此 :class:`LocalStore` 以载荷类型为泛型参数::

    LocalStore[pd.DataFrame]   snapshot 的各张表        parquet / excel / emit
    LocalStore[str]            LLM 的 prompt 与响应原文  text

「哪一类」因此是类型而不是注释，两者也永不混装在同一个列表里。协议方法
不随载荷类型增长——结构知识归 snapshot，格式知识归 store，落盘时机归
context，这条分工不变。

------------------------------------------------------------------------
只写 vs 可回读
------------------------------------------------------------------------
``ExcelStore`` 刻意**不实现** :class:`ReadableLocalStore` —— 从类型上杜绝
「拿交付物当回读源」。旧版所有类型补救代码的根源就是拿 xlsx 当回读源：
``list`` 读回来变成 ``"['a', 'b']"``、numpy 数组变成 ``"['a' 'b']"``。

parquet 原生保 ``list<string>`` 与 ``bool``；它把 list 读回成 numpy array，
但那是**确定性转换**，由 snapshot 的 ``from_frames`` 归一，不是靠猜。

``TextStore`` 可回读：LLM 的响应缓存就是断点续跑与录制回放的全部实现，
文本进出无格式损耗，不存在 xlsx 那种回读陷阱。

------------------------------------------------------------------------
key 与文件名分离
------------------------------------------------------------------------
``_safe()`` 会把非 ``[0-9A-Za-z_.-]`` 的连续字符压成一个 ``_``，两个中文
表名会撞成同一个文件、静默覆盖。因此文件树 store 共用 :class:`_KeyIndex`
维护 ``_index.json``（key → 文件名），**回读不从 key 反推文件名**，碰撞时
加消歧后缀。

------------------------------------------------------------------------
异步边界
------------------------------------------------------------------------
接口一律 async；pandas / pyarrow / openpyxl 都是同步库，实现内部用
``asyncio.to_thread`` 下线程。**不写「async def 里不 await」的假异步** ——
那比同步更危险，看起来不阻塞实际阻塞。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Generic, Mapping, Sequence, TypeVar

import pandas as pd

logger = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^0-9A-Za-z_.\-]+")

#: 载荷类型。目前只有两种取值：``pd.DataFrame`` 与 ``str``。
P = TypeVar("P")


def _safe(key: str) -> str:
    """把任意 key 变成安全文件名。可能碰撞，由 key 索引消歧。"""
    return _UNSAFE.sub("_", str(key)).strip("_") or "_"


# ===========================================================================
# 协议
# ===========================================================================


async def _finish_write(task):
    """A cancelled write must finish before its owner reports it stopped."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


_pending_writes: ContextVar[set[asyncio.Task] | None] = ContextVar(
    "profile_file_writes", default=None
)


@asynccontextmanager
async def complete_file_writes():
    """Join dispatched writes even if a parallel algorithm branch exits first.

    asyncio.gather can propagate one child's cancellation before siblings finish.
    Track only file writes here, so read/LLM work remains cancellable.
    """
    pending: set[asyncio.Task] = set()
    token = _pending_writes.set(pending)
    try:
        yield
    finally:
        try:
            if pending:
                await _finish_write(asyncio.gather(*pending, return_exceptions=True))
        finally:
            _pending_writes.reset(token)


async def _write_thread(func, *args):
    task = asyncio.create_task(asyncio.to_thread(func, *args))
    pending = _pending_writes.get()
    if pending is not None:
        pending.add(task)
        task.add_done_callback(pending.discard)
    return await _finish_write(task)


class LocalStore(ABC, Generic[P]):
    """一个落盘去向。**只写。**"""

    #: 供日志与装配识别
    kind: str = "local"

    @abstractmethod
    async def write(self, key: str, payload: P) -> None: ...

    @abstractmethod
    async def write_meta(self, name: str, payload: Mapping[str, Any]) -> None: ...

    async def clear(self) -> None:
        """清空。fixture 重录用；默认无操作。"""


class ReadableLocalStore(LocalStore[P]):
    """可回读。缓存命中与续跑都依赖它。"""

    @abstractmethod
    async def read(self, key: str) -> P | None: ...

    @abstractmethod
    async def read_meta(self, name: str) -> dict[str, Any] | None: ...

    @abstractmethod
    async def keys(self) -> set[str]: ...


def first_readable(
    stores: Sequence[LocalStore[P]] | None,
) -> ReadableLocalStore[P] | None:
    """取第一个可回读的 store 作为缓存源；没有则每次都走 fetch。"""
    for store in stores or ():
        if isinstance(store, ReadableLocalStore):
            return store
    return None


# ===========================================================================
# 文件树 store 的共用骨架
# ===========================================================================


class _KeyIndex:
    """``key → 文件名`` 的权威映射，落在 ``{root}/_index.json``。

    **分配带锁。** ``LlmRunner`` 并发调用 LLM，因此并发写同一个 store，而
    store 的写实现跑在 ``asyncio.to_thread`` 的真并发线程里——读改写同一份
    索引不加锁会丢更新。context 那一侧是串行 for-loop 写 key，这条锁是为
    LLM 传输层加的。
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._index: dict[str, str] | None = None
        self._lock = threading.Lock()

    def _path(self) -> Path:
        return self.root / "_index.json"

    def _load(self) -> dict[str, str]:
        """调用方必须已持锁。"""
        if self._index is not None:
            return self._index
        path = self._path()
        if path.exists():
            try:
                self._index = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("key 索引损坏，按空处理 %s: %s", path, exc)
                self._index = {}
        else:
            self._index = {}
        return self._index

    def allocate(self, key: str) -> str:
        """取或分配文件名。碰撞时加 ``__2`` / ``__3`` 消歧。"""
        with self._lock:
            index = self._load()
            if key in index:
                return index[key]
            base = _safe(key)
            taken = set(index.values())
            name, n = base, 1
            while name in taken:
                n += 1
                name = f"{base}__{n}"
            index[key] = name
            self._path().parent.mkdir(parents=True, exist_ok=True)
            self._path().write_text(
                json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return name

    def lookup(self, key: str) -> str | None:
        with self._lock:
            return self._load().get(key)

    def known(self) -> set[str]:
        with self._lock:
            return set(self._load())

    def forget(self) -> None:
        with self._lock:
            self._index = None


class _FileTreeStore(ReadableLocalStore[P]):
    """文件树 + key 索引 + JSON 元信息侧车。

    布局::

        {root}/_index.json          key → 文件名（**权威**，回读不反推）
        {root}/_meta/{name}.json    manifest / 指纹 / 调用元信息
        {root}/{文件名}{suffix}

    子类只需给出单个载荷的读写与后缀；索引、元信息、清空全在这里。
    """

    #: 文件后缀，含点
    suffix: str = ""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._keys = _KeyIndex(self.root)

    # -- 子类实现 --------------------------------------------------------

    @abstractmethod
    def _dump(self, path: Path, payload: P) -> None: ...

    @abstractmethod
    def _parse(self, path: Path) -> P: ...

    # -- 写 --------------------------------------------------------------

    async def write(self, key: str, payload: P) -> None:
        await _write_thread(self._write_sync, key, payload)

    def _write_sync(self, key: str, payload: P) -> None:
        path = self.root / f"{self._keys.allocate(key)}{self.suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._dump(path, payload)

    async def write_meta(self, name: str, payload: Mapping[str, Any]) -> None:
        await _write_thread(self._write_meta_sync, name, dict(payload))

    def _write_meta_sync(self, name: str, payload: dict[str, Any]) -> None:
        path = self.root / "_meta" / f"{_safe(name)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    # -- 读 --------------------------------------------------------------

    async def read(self, key: str) -> P | None:
        return await asyncio.to_thread(self._read_sync, key)

    def _read_sync(self, key: str) -> P | None:
        name = self._keys.lookup(key)
        if name is None:
            return None
        path = self.root / f"{name}{self.suffix}"
        if not path.exists():
            logger.warning("key 索引指向不存在的文件 %s → %s", key, path)
            return None
        try:
            return self._parse(path)
        except Exception as exc:
            logger.warning("%s 读取失败，按未命中处理 %s: %s", self.kind, path, exc)
            return None

    async def read_meta(self, name: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._read_meta_sync, name)

    def _read_meta_sync(self, name: str) -> dict[str, Any] | None:
        path = self.root / "_meta" / f"{_safe(name)}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("元信息读取失败 %s: %s", path, exc)
            return None

    async def keys(self) -> set[str]:
        return await asyncio.to_thread(self._keys.known)

    async def clear(self) -> None:
        await _write_thread(self._clear_sync)

    def _clear_sync(self) -> None:
        import shutil

        if self.root.exists():
            shutil.rmtree(self.root)
        self._keys.forget()


# ===========================================================================
# parquet：默认缓存后端
# ===========================================================================


class ParquetStore(_FileTreeStore[pd.DataFrame]):
    """DataFrame 的缓存与回读后端。"""

    kind = "parquet"
    suffix = ".parquet"

    def __init__(self, root: str | Path, *, compression: str) -> None:
        """``compression``：**必填** —— 参数的唯一的家在
        ``config.store_parquet_compression``，这里不留第二个默认值。
        """
        super().__init__(root)
        self._compression = compression

    def _dump(self, path: Path, payload: pd.DataFrame) -> None:
        payload.to_parquet(path, index=False, compression=self._compression)

    def _parse(self, path: Path) -> pd.DataFrame:
        return pd.read_parquet(path)


# ===========================================================================
# text：LLM 的 prompt 与响应原文
# ===========================================================================


class TextStore(_FileTreeStore[str]):
    """纯文本，**点开就是原文**。

    LLM 的 prompt 与响应是给人读的：JSON 会把多行文本转义成一行 ``\\n``
    串，parquet 更是二进制。因此正文走 ``.txt``，结构化元信息走已有的
    ``write_meta`` / ``read_meta``（JSON 侧车）——两类需求各归其位，协议
    一个方法都不用加。

    可回读：LLM 响应缓存就是断点续跑与录制回放的全部实现。
    """

    kind = "text"
    suffix = ".txt"

    def _dump(self, path: Path, payload: str) -> None:
        path.write_text(str(payload), encoding="utf-8")

    def _parse(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")


# ===========================================================================
# excel：对外交付 / 人工核验
# ===========================================================================


class ExcelStore(LocalStore[pd.DataFrame]):
    """**只写不读。** xlsx 是给人看的，不是给程序读的。

    ``list`` 单元格转成逗号拼接——这正是它不能当回读源、也不实现
    ``read`` 的原因。

    不走 key 索引：交付物撞名可以接受，回读源不行。
    """

    kind = "excel"

    def __init__(self, root: str | Path, *, max_rows: int) -> None:
        """``max_rows``：xlsx 单表行数上限，超出截断。**必填** ——
        它是参数，唯一的家在 ``config.store_excel_max_rows``。
        """
        self.root = Path(root)
        self._max_rows = int(max_rows)

    async def write(self, key: str, payload: pd.DataFrame) -> None:
        await _write_thread(self._write_sync, key, payload)

    def _write_sync(self, key: str, payload: pd.DataFrame) -> None:
        path = self.root / f"{_safe(key)}.xlsx"
        path.parent.mkdir(parents=True, exist_ok=True)
        flat = payload.head(self._max_rows).copy()
        if len(payload) > self._max_rows:
            logger.info("%s 超过 xlsx 行数上限，导出前 %s 行", key, self._max_rows)
        for column in flat.columns:
            if flat[column].map(lambda v: isinstance(v, (list, tuple, set))).any():
                flat[column] = flat[column].map(
                    lambda v: (
                        ",".join(map(str, v))
                        if isinstance(v, (list, tuple, set))
                        else v
                    )
                )
        flat.to_excel(path, index=False)

    async def write_meta(self, name: str, payload: Mapping[str, Any]) -> None:
        """元信息对人无用，跳过。"""


# ===========================================================================
# emit：吐给编排层（流式）
# ===========================================================================


class EmitStore(LocalStore[pd.DataFrame]):
    """把产出吐给编排层。**只写。**

    它就是流式 emit 的全部实现——挂上就有，不挂就没有。不需要另立
    ``ArtifactSink`` 那层间接。

    ``emit`` 由**编排层注入**：它负责投影成 ``Candidate*`` 模型、跨回事件
    循环、落 staging 表。算法层只见 ``LocalStore`` 协议。

    emit 失败**不抛**——已经算好的结果不该因为吐不出去而丢失，落盘那一路
    仍然成功。
    """

    kind = "emit"

    def __init__(
        self,
        emit: Callable[[str, str, pd.DataFrame], Any],
        *,
        context_name: str = "",
    ) -> None:
        self._emit = emit
        self._context_name = context_name

    async def write(self, key: str, payload: pd.DataFrame) -> None:
        try:
            result = self._emit(self._context_name, key, payload)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.warning(
                "emit 失败（%s/%s），落盘不受影响: %s", self._context_name, key, exc
            )

    async def write_meta(self, name: str, payload: Mapping[str, Any]) -> None:
        """emit 不吐元信息。"""


__all__ = [
    "EmitStore",
    "ExcelStore",
    "LocalStore",
    "ParquetStore",
    "ReadableLocalStore",
    "TextStore",
    "first_readable",
]
