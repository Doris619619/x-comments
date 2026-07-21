"""
本文件负责协调单个闲鱼账号在多进程之间的串行访问。

它属于 services 模块：先用进程内 ``asyncio.Lock`` 避免同一事件循环内竞争，再在
PostgreSQL 上持有 session-level advisory lock，避免 API 与 scheduler worker 两个容器
同时使用同一份登录态。SQLite 等离线测试数据库会安全退化为仅使用进程内锁。

本文件不启动 Playwright、不读取登录态、不执行业务任务，也不负责绕过登录、验证码或
平台风控。数据库连接只用于 advisory lock，并在退出、取消或连接异常时显式释放或失效。
"""

import asyncio
import hashlib
import threading
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from typing import Protocol, TypeAlias

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

PRIMARY_XIANYU_ACCOUNT_RESOURCE = "x-comments:xianyu:primary-account"


class AccountAccessGuard(Protocol):
    """
    定义闲鱼账号独占访问的异步上下文接口。

    实现必须在 ``hold`` 上下文存续期间保证账号访问串行化；进入失败时异常向上抛出，
    离开时必须释放已获得的资源。协议本身不访问数据库或页面。
    """

    def hold(self) -> AbstractAsyncContextManager[None]:
        """
        返回一次账号独占租约的异步上下文。

        无输入；成功进入后返回空值；锁获取失败时向上抛出异常；副作用是临时占用账号锁。
        """


AccountGuardInput: TypeAlias = AccountAccessGuard | asyncio.Lock


def stable_advisory_key(resource_name: str) -> int:
    """
    将账号资源名稳定映射为 PostgreSQL advisory lock 使用的有符号 64 位整数。

    参数 ``resource_name`` 必须是非空稳定标识；返回跨进程、跨重启一致的整数键；空值会
    抛出 ``ValueError``。函数只做确定性哈希，不读取数据库且没有外部副作用。
    """

    normalized = resource_name.strip()
    if not normalized:
        raise ValueError("闲鱼账号锁资源名不能为空")
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


class AsyncioLockAccountGuard:
    """
    将已有 ``asyncio.Lock`` 适配为统一账号访问协议。

    该适配器供离线测试和现有调用方注入；只保证当前进程串行，不提供跨进程互斥。
    """

    def __init__(self, lock: asyncio.Lock) -> None:
        """
        保存调用方提供的进程内锁。

        参数为 ``asyncio.Lock``；无返回和异常；副作用仅为保存引用，不会立即获取锁。
        """

        self.lock = lock

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        """
        在已有进程内锁中提供一次独占访问。

        无输入；上下文内返回空值；等待期间可被取消；退出时由 ``asyncio.Lock`` 可靠释放。
        """

        async with self.lock:
            yield


def normalize_account_guard(account_guard: AccountGuardInput | None) -> AccountAccessGuard:
    """
    将可选统一 guard 或旧式 ``asyncio.Lock`` 转换为统一协议。

    参数为空时创建新的进程内 guard；传入 ``asyncio.Lock`` 时创建薄适配器；传入协议实现
    时原样返回。函数不获取锁，除对象创建外没有外部副作用。
    """

    if account_guard is None:
        return AsyncioLockAccountGuard(asyncio.Lock())
    if isinstance(account_guard, asyncio.Lock):
        return AsyncioLockAccountGuard(account_guard)
    return account_guard


class XianyuAccountGuard:
    """
    使用进程内锁和 PostgreSQL session advisory lock 串行化账号访问。

    同一实例先限制进程内并发；不同容器使用相同资源名和数据库时，再由 PostgreSQL 锁
    互斥。非 PostgreSQL 引擎只使用进程内锁，方便 SQLite 离线测试。
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        resource_name: str = PRIMARY_XIANYU_ACCOUNT_RESOURCE,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        """
        保存数据库引擎、稳定锁键与轮询间隔。

        参数为已绑定引擎的会话工厂、账号资源名和正数轮询秒数；无返回；无绑定引擎或
        非正间隔时抛出 ``ValueError``。初始化不连接数据库，也不会获取锁。
        """

        if poll_interval_seconds <= 0:
            raise ValueError("账号锁轮询间隔必须大于零")
        bind = session_factory.kw.get("bind")
        if not isinstance(bind, Engine):
            raise ValueError("账号锁需要绑定 SQLAlchemy Engine 的会话工厂")
        self._engine = bind
        self._process_lock = asyncio.Lock()
        self._advisory_key = stable_advisory_key(resource_name)
        self._poll_interval_seconds = poll_interval_seconds
        self._uses_postgresql = bind.dialect.name == "postgresql"

    @property
    def advisory_key(self) -> int:
        """
        返回当前账号资源对应的稳定 advisory key。

        无输入；返回有符号 64 位整数；不抛出异常且没有副作用，主要用于诊断与离线测试。
        """

        return self._advisory_key

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        """
        获取进程内锁，并在 PostgreSQL 运行时再获取跨进程账号锁。

        无输入；上下文内返回空值；数据库错误向上抛出；退出、任务取消或异常时都会释放
        advisory lock 并关闭专用连接。SQLite 仅产生进程内锁副作用。
        """

        async with self._process_lock:
            if not self._uses_postgresql:
                yield
                return
            connection = await self._acquire_postgresql_lock()
            try:
                yield
            finally:
                await self._release_postgresql_lock(connection)

    async def _acquire_postgresql_lock(self) -> Connection:
        """
        在专用数据库连接上轮询获取 session-level advisory lock。

        无显式输入；返回持锁的 SQLAlchemy 连接；数据库异常向上抛出。等待任务被取消时，
        会通知后台线程停止，并对可能刚获得锁的连接执行解锁和关闭。
        """

        cancel_event = threading.Event()
        acquire_task = asyncio.create_task(
            asyncio.to_thread(self._acquire_postgresql_lock_sync, cancel_event)
        )
        try:
            connection = await asyncio.shield(acquire_task)
        except asyncio.CancelledError:
            cancel_event.set()
            connection = None
            with suppress(Exception):
                connection = await acquire_task
            if connection is not None:
                await self._release_postgresql_lock(connection)
            raise
        if connection is None:
            raise asyncio.CancelledError
        return connection

    def _acquire_postgresql_lock_sync(self, cancel_event: threading.Event) -> Connection | None:
        """
        在线程中建立专用连接并用非阻塞 SQL 轮询 advisory lock。

        参数为取消事件；成功返回持锁连接，取消返回 ``None``，数据库异常向上抛出。未获得
        锁的连接总会关闭；连接异常时会先失效，确保 PostgreSQL 会话不能残留锁。
        """

        connection: Connection | None = None
        acquired = False
        try:
            connection = self._engine.connect().execution_options(isolation_level="AUTOCOMMIT")
            while not cancel_event.is_set():
                acquired = bool(
                    connection.execute(
                        text("SELECT pg_try_advisory_lock(:lock_key)"),
                        {"lock_key": self._advisory_key},
                    ).scalar_one()
                )
                if acquired:
                    return connection
                cancel_event.wait(self._poll_interval_seconds)
            return None
        except Exception:
            if connection is not None:
                connection.invalidate()
            raise
        finally:
            if connection is not None and not acquired:
                connection.close()

    async def _release_postgresql_lock(self, connection: Connection) -> None:
        """
        在线程中显式解锁并关闭持锁的专用数据库连接。

        参数为持锁连接；无返回；正常解锁错误向上抛出。若释放期间再次收到取消，会等待
        清理线程结束后再传播取消，防止池化连接携带 session lock 返回连接池。
        """

        release_task = asyncio.create_task(
            asyncio.to_thread(self._release_postgresql_lock_sync, connection)
        )
        try:
            await asyncio.shield(release_task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await release_task
            raise

    def _release_postgresql_lock_sync(self, connection: Connection) -> None:
        """
        执行 PostgreSQL 解锁；失败时使物理连接失效后再关闭。

        参数为专用连接；无返回；SQL 错误会在完成失效与关闭后重新抛出。副作用是释放
        session advisory lock，并确保无法确认解锁的连接不会带锁回到连接池。
        """

        try:
            unlocked = bool(
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": self._advisory_key},
                ).scalar_one()
            )
            if not unlocked:
                connection.invalidate()
                raise RuntimeError("PostgreSQL 未确认闲鱼账号 advisory lock 已释放")
        except Exception:
            connection.invalidate()
            raise
        finally:
            connection.close()
