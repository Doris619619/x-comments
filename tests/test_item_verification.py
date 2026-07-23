"""
本文件离线测试商城结算前的单商品核验 API 和安全状态映射。

它使用内存 SQLite 与可注入假核验器，不启动浏览器、不读取登录态，也不访问真实闲鱼。
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright._impl._errors import TargetClosedError
from playwright.async_api import Browser, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.crawler.item_verifier import (
    DETAIL_PRICE_WAIT_MAX_MILLISECONDS,
    XianyuItemVerifier,
    _close_browser_after_verification,
    _close_page_after_verification,
    find_explicit_unavailable_signal,
    page_matches_target,
    parse_single_price_text,
)
from app.crawler.selectors import DETAIL_PRIMARY_PRICE_SELECTOR
from app.jobs.worker import CrawlWorker
from app.repositories.items import ItemRepository
from app.schemas.item import ParsedItem
from app.services.item_verification import (
    LiveVerificationResult,
    LiveVerificationStatus,
    VerificationTarget,
)

VERIFY_HEADERS = {"Authorization": "Bearer offline-test-token-0123456789abcdef"}


class FakeVerifier:
    """
    返回预设实时结果并记录调用次数的离线核验器。

    输入预设结果；verify 返回该结果；副作用仅为在内存列表记录目标。
    """

    def __init__(self, result: LiveVerificationResult) -> None:
        """保存预设结果并初始化空调用列表；无异常和外部副作用。"""

        self.result = result
        self.calls: list[VerificationTarget] = []

    async def verify(self, target: VerificationTarget) -> LiveVerificationResult:
        """记录一个目标并返回预设结果；不访问页面或网络。"""

        self.calls.append(target)
        return self.result


class RaisingVerifier:
    """
    模拟核验实现内部异常的离线核验器。

    无初始化输入；verify 总是抛出 RuntimeError；不访问页面或网络。
    """

    async def verify(self, target: VerificationTarget) -> LiveVerificationResult:
        """接收目标后抛出固定异常，用于验证服务失败关闭；无外部副作用。"""

        raise RuntimeError(f"offline verifier failure for {target.item_id}")


class SerialProbeVerifier(XianyuItemVerifier):
    """
    用内存计数验证账号锁串行化，不启动 Playwright。

    输入配置和共享锁；核验返回固定可售结果；副作用仅为更新测试计数。
    """

    def __init__(self, settings: Settings, account_lock: asyncio.Lock) -> None:
        """保存配置与共享锁并初始化并发计数；无外部副作用。"""

        super().__init__(settings, account_lock)
        self.active = 0
        self.maximum_active = 0

    async def _verify_once(
        self, target: VerificationTarget, state_path: Path
    ) -> LiveVerificationResult:
        """在短暂异步等待期间记录并发量并返回固定结果；不读取登录态内容。"""

        del state_path
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            await asyncio.sleep(0.01)
            return LiveVerificationResult(
                LiveVerificationStatus.AVAILABLE,
                Decimal("12.50"),
                f"probe_{target.item_id}",
            )
        finally:
            self.active -= 1


class FakeDelayedDetailPage:
    """
    记录详情价格等待参数，并可模拟客户端渲染超时。

    输入是否超时；wait_for_selector 无网络副作用，只保存调用并按配置返回或抛出。
    """

    def __init__(self, *, should_timeout: bool) -> None:
        """保存超时模式并初始化空调用列表；不访问浏览器或网络。"""

        self.should_timeout = should_timeout
        self.calls: list[tuple[str, str, float]] = []

    async def wait_for_selector(
        self,
        selector: str,
        *,
        state: str,
        timeout: float,
    ) -> object | None:
        """
        记录选择器、状态和等待时限。

        输入 Playwright 兼容参数；正常返回占位对象，超时模式抛出固定异常；无外部副作用。
        """

        self.calls.append((selector, state, timeout))
        if self.should_timeout:
            raise PlaywrightTimeoutError("offline delayed detail fixture")
        return object()


class FakeAlreadyClosedTarget:
    """
    模拟 Playwright 目标已先行关闭、再次清理会抛出 TargetClosedError 的对象。

    无初始化输入；close 只记录次数并抛出固定异常，不访问浏览器或网络。
    """

    def __init__(self) -> None:
        """初始化关闭调用计数；无异常和外部副作用。"""

        self.close_calls = 0

    async def close(self) -> None:
        """记录一次重复关闭并抛出目标已关闭异常；不访问外部资源。"""

        self.close_calls += 1
        raise TargetClosedError("offline target already closed")


class FakeUnexpectedCloseError:
    """
    模拟清理阶段出现非目标关闭异常的对象。

    无初始化输入；close 抛出 RuntimeError，用于确认修复不会吞掉其他清理故障。
    """

    async def close(self) -> None:
        """抛出固定的非 Playwright 关闭异常；不访问外部资源。"""

        raise RuntimeError("offline unexpected cleanup failure")


class TargetClosedProbeVerifier(XianyuItemVerifier):
    """
    模拟导航业务阶段发生 TargetClosedError 的核验器。

    输入配置；核验不会启动 Playwright，只用于确认业务阶段异常继续失败关闭。
    """

    async def _verify_once(
        self, target: VerificationTarget, state_path: Path
    ) -> LiveVerificationResult:
        """接收目标和登录态路径后抛出固定异常；不读取文件内容或访问网络。"""

        del target, state_path
        raise TargetClosedError("offline navigation target closed")


def seed_item(session_factory: sessionmaker[Session], item_id: str = "31001") -> None:
    """
    在当前测试的内存数据库写入一个可供核验的公开商品。

    输入会话工厂和可选商品 ID；无返回；副作用仅限内存 SQLite。
    """

    item = ParsedItem(
        item_id=item_id,
        title="核验测试商品",
        price=Decimal("12.50"),
        image_url=None,
        item_url=f"https://www.goofish.com/item?id={item_id}",
        location="上海",
    )
    with session_factory() as session:
        ItemRepository(session).upsert_many("核验测试", [item], datetime.now(UTC))


@pytest.mark.parametrize(
    ("live_result", "expected_status", "expected_price"),
    [
        (
            LiveVerificationResult(
                LiveVerificationStatus.AVAILABLE, Decimal("12.50"), "listing_available"
            ),
            "available",
            "12.50",
        ),
        (
            LiveVerificationResult(
                LiveVerificationStatus.UNAVAILABLE,
                None,
                "listing_explicitly_unavailable",
            ),
            "unavailable",
            None,
        ),
        (
            LiveVerificationResult(
                LiveVerificationStatus.AVAILABLE, Decimal("13.00"), "listing_available"
            ),
            "price_changed",
            "13.00",
        ),
        (
            LiveVerificationResult(
                LiveVerificationStatus.BLOCKED, None, "risk_control_blocked"
            ),
            "blocked",
            None,
        ),
        (
            LiveVerificationResult(
                LiveVerificationStatus.UNKNOWN, None, "listing_price_not_found"
            ),
            "unknown",
            None,
        ),
    ],
)
def test_verify_api_returns_all_five_contract_statuses(
    client: TestClient,
    session_factory: sessionmaker[Session],
    live_result: LiveVerificationResult,
    expected_status: str,
    expected_price: str | None,
) -> None:
    """
    验证单次注入结果稳定映射为商城约定的五种状态。

    输入内存客户端、数据库与参数；断言失败抛出 AssertionError；不访问真实网络。
    """

    seed_item(session_factory)
    verifier = FakeVerifier(live_result)
    application = cast(FastAPI, client.app)
    application.state.item_verifier = verifier

    response = client.post(
        "/api/v1/items/31001/verify",
        json={"expected_price": "12.50", "currency": "CNY", "context": "checkout"},
        headers=VERIFY_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == expected_status
    assert body["current_price"] == expected_price
    assert body["verified_at"]
    assert body["reason_code"]
    assert body["request_id"]
    assert verifier.calls == [VerificationTarget(item_id="31001")]


def test_verify_api_returns_404_without_live_access(
    client: TestClient,
) -> None:
    """
    验证本地数据库没有目标商品时返回 404 且不调用实时核验器。

    输入空库客户端；断言失败抛出 AssertionError；不访问页面或网络。
    """

    verifier = FakeVerifier(
        LiveVerificationResult(
            LiveVerificationStatus.AVAILABLE, Decimal("12.50"), "listing_available"
        )
    )
    application = cast(FastAPI, client.app)
    application.state.item_verifier = verifier

    response = client.post(
        "/api/v1/items/does-not-exist/verify",
        json={"expected_price": "12.50", "currency": "CNY", "context": "checkout"},
        headers=VERIFY_HEADERS,
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "商品不存在"}
    assert verifier.calls == []


def test_verify_api_rejects_invalid_contract_fields(client: TestClient) -> None:
    """
    验证非人民币币种和非结算场景在访问数据库与页面前被拒绝。

    输入内存客户端；断言失败抛出 AssertionError；无外部副作用。
    """

    response = client.post(
        "/api/v1/items/31001/verify",
        json={"expected_price": "12.50", "currency": "JPY", "context": "preview"},
        headers=VERIFY_HEADERS,
    )
    assert response.status_code == 422


def test_verify_service_converts_unexpected_error_to_unknown(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """
    验证注入核验器异常不会产生 500 或误判商品可售。

    输入内存客户端和数据库；断言失败抛出 AssertionError；不访问真实网络。
    """

    seed_item(session_factory)
    application = cast(FastAPI, client.app)
    application.state.item_verifier = RaisingVerifier()

    response = client.post(
        "/api/v1/items/31001/verify",
        json={"expected_price": "12.50", "currency": "CNY", "context": "checkout"},
        headers=VERIFY_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "unknown"
    assert response.json()["reason_code"] == "verification_internal_error"


def test_visible_page_classification_is_conservative() -> None:
    """
    验证明确信号才判不可售，且模糊文本或多个金额不会产生可售价格。

    无输入；断言失败抛出 AssertionError；只执行纯文本解析。
    """

    assert find_explicit_unavailable_signal("卖家稍后发货") is None
    assert find_explicit_unavailable_signal("来晚了，商品已售出") == "商品已售出"
    assert parse_single_price_text("¥12.50") == Decimal("12.50")
    assert parse_single_price_text("现价 ¥12.50 原价 ¥20.00") is None
    assert parse_single_price_text("-1.00") is None
    assert page_matches_target("https://www.goofish.com/item?id=31001", "31001")
    assert not page_matches_target("https://www.goofish.com/search?q=31001", "31001")
    assert not page_matches_target("https://example.com/item?id=31001", "31001")


@pytest.mark.asyncio
@pytest.mark.parametrize("should_timeout", [False, True])
async def test_detail_price_wait_is_bounded_and_keeps_primary_selector(
    should_timeout: bool,
) -> None:
    """
    验证延迟详情只等待主商品价格，且节点缺失不会触发导航重试或异常外泄。

    输入超时模式；断言失败抛出 AssertionError；只使用内存假页面，不访问闲鱼。
    """

    settings = Settings(xianyu_verify_timeout_seconds=12)
    verifier = XianyuItemVerifier(settings)
    page = FakeDelayedDetailPage(should_timeout=should_timeout)

    await verifier._wait_for_primary_price(cast(Page, page))  # noqa: SLF001

    assert page.calls == [
        (
            DETAIL_PRIMARY_PRICE_SELECTOR,
            "visible",
            DETAIL_PRICE_WAIT_MAX_MILLISECONDS,
        )
    ]
    assert "item-main-info--" in DETAIL_PRIMARY_PRICE_SELECTOR
    assert "value--" in DETAIL_PRIMARY_PRICE_SELECTOR


@pytest.mark.asyncio
async def test_cleanup_target_closed_does_not_mask_verification_result() -> None:
    """
    验证页面或浏览器已经关闭时，清理动作不会覆盖此前得到的商品核验结果。

    无外部输入；断言两个清理助手各执行一次且正常返回；不启动真实浏览器或网络。
    """

    page = FakeAlreadyClosedTarget()
    browser = FakeAlreadyClosedTarget()

    await _close_page_after_verification(cast(Page, page))
    await _close_browser_after_verification(cast(Browser, browser))

    assert page.close_calls == 1
    assert browser.close_calls == 1


@pytest.mark.asyncio
async def test_cleanup_keeps_unexpected_close_error_visible() -> None:
    """
    验证修复只忽略 TargetClosedError，不会静默吞掉其他清理异常。

    无外部输入；断言 RuntimeError 原样抛出；不启动真实浏览器或网络。
    """

    target = FakeUnexpectedCloseError()

    with pytest.raises(RuntimeError, match="unexpected cleanup failure"):
        await _close_page_after_verification(cast(Page, target))


@pytest.mark.asyncio
async def test_operational_target_closed_returns_specific_unknown() -> None:
    """
    验证业务核验阶段关闭页面时仍按失败关闭处理，并返回可诊断的未知状态。

    输入离线夹具路径；断言状态、价格和原因码；不启动真实浏览器或网络。
    """

    state_path = Path(__file__).parent / "fixtures" / "search_response.json"
    settings = Settings(
        xianyu_storage_state_path=str(state_path),
        xianyu_verify_timeout_seconds=5,
    )
    verifier = TargetClosedProbeVerifier(settings)

    result = await verifier.verify(VerificationTarget(item_id="31001"))

    assert result.status is LiveVerificationStatus.UNKNOWN
    assert result.current_price is None
    assert result.reason_code == "verification_target_closed"


def test_verify_api_requires_configured_bearer_token(client: TestClient) -> None:
    """
    验证核验接口拒绝缺失或错误令牌，并在服务端未配置令牌时安全关闭。

    输入测试客户端；断言失败抛出 AssertionError；只临时调整应用内存配置。
    """

    payload = {"expected_price": "12.50", "currency": "CNY", "context": "checkout"}
    missing = client.post("/api/v1/items/31001/verify", json=payload)
    wrong = client.post(
        "/api/v1/items/31001/verify",
        json=payload,
        headers={"Authorization": "Bearer wrong-token"},
    )
    application = cast(FastAPI, client.app)
    application.state.item_verification_token = None
    disabled = client.post(
        "/api/v1/items/31001/verify",
        json=payload,
        headers=VERIFY_HEADERS,
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert disabled.status_code == 503


@pytest.mark.asyncio
async def test_account_lock_serializes_verification_and_is_shared_with_worker(
    session_factory: sessionmaker[Session],
) -> None:
    """
    验证并发核验最多一个进入页面层，且采集 worker 可复用同一账号锁。

    输入内存会话工厂；断言失败抛出 AssertionError；不启动浏览器或网络。
    """

    state_path = Path(__file__).parent / "fixtures" / "search_response.json"
    settings = Settings(
        xianyu_storage_state_path=str(state_path),
        xianyu_verify_timeout_seconds=5,
    )
    account_lock = asyncio.Lock()
    verifier = SerialProbeVerifier(settings, account_lock)
    worker = CrawlWorker(session_factory, settings, account_lock)

    results = await asyncio.gather(
        verifier.verify(VerificationTarget(item_id="31001")),
        verifier.verify(VerificationTarget(item_id="31002")),
    )

    assert [result.status for result in results] == [
        LiveVerificationStatus.AVAILABLE,
        LiveVerificationStatus.AVAILABLE,
    ]
    assert verifier.maximum_active == 1
    assert verifier.account_lock is account_lock
    assert worker.crawler.account_lock is account_lock
