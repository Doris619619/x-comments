"""
本文件离线验证闲鱼账号 guard 的确定性键值、进程内串行和取消清理。

它属于 tests 服务层测试，使用内存 SQLite，因此不会执行 PostgreSQL advisory SQL、不会
启动 Playwright，也不会访问真实闲鱼或读取登录态。
"""

import asyncio

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.services.xianyu_account_guard import XianyuAccountGuard, stable_advisory_key


def test_stable_advisory_key_is_repeatable_and_resource_specific() -> None:
    """
    验证同一账号资源跨调用得到相同有符号 64 位键，不同资源不会得到该测试中的同一键。

    无输入；断言失败抛出 ``AssertionError``；只运行纯哈希计算且没有外部副作用。
    """

    first = stable_advisory_key("x-comments:xianyu:primary-account")
    second = stable_advisory_key("x-comments:xianyu:primary-account")
    other = stable_advisory_key("x-comments:xianyu:secondary-account")

    assert first == second
    assert first == 4239057377912453295
    assert first != other
    assert -(2**63) <= first < 2**63


@pytest.mark.asyncio
async def test_sqlite_guard_serializes_same_process(
    session_factory: sessionmaker[Session],
) -> None:
    """
    验证 SQLite 退化模式仍通过进程内锁把并发临界区限制为一个。

    参数为内存 SQLite 会话工厂；断言失败抛出 ``AssertionError``；副作用仅为更新内存计数。
    """

    guard = XianyuAccountGuard(session_factory)
    active = 0
    maximum_active = 0

    async def enter_probe() -> None:
        """
        进入 guard 后短暂让出事件循环并记录同时进入数量。

        无输入和返回；断言由外层完成；副作用仅为修改当前测试的内存计数。
        """

        nonlocal active, maximum_active
        async with guard.hold():
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                await asyncio.sleep(0.01)
            finally:
                active -= 1

    await asyncio.gather(*(enter_probe() for _ in range(5)))

    assert maximum_active == 1


@pytest.mark.asyncio
async def test_sqlite_guard_releases_process_lock_after_cancellation(
    session_factory: sessionmaker[Session],
) -> None:
    """
    验证持锁协程取消后，后续任务仍能获取 SQLite 退化模式的进程内锁。

    参数为内存 SQLite 会话工厂；超时或断言失败时测试失败；不访问数据库表和外部网络。
    """

    guard = XianyuAccountGuard(session_factory)
    entered = asyncio.Event()

    async def cancellable_holder() -> None:
        """
        获取 guard 后等待，供测试从外部取消。

        无输入和正常返回；取消时传播 ``CancelledError``；副作用是短暂占用进程内锁。
        """

        async with guard.hold():
            entered.set()
            await asyncio.sleep(60)

    holder = asyncio.create_task(cancellable_holder())
    await entered.wait()
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder

    async with asyncio.timeout(1):
        async with guard.hold():
            pass
