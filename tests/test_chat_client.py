"""
本文件离线验证闲鱼聊天页面适配层的身份锁定、消息指纹和失败关闭发送边界。

它属于 crawler 单元测试，只使用内存 Fake Page/Locator 和 Fake AccountAccessGuard，绝不
启动 Playwright 浏览器、不访问网络、不读取本地登录态，也不触发真实闲鱼聊天或交易。
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import cast

import pytest
from playwright.async_api import Page

from app.crawler.chat_client import (
    ChatBinding,
    ChatSafetyError,
    PolicyAllowedDraft,
    XianyuChatClient,
    build_message_fingerprint,
    item_url_matches_binding,
)
from app.crawler.chat_selectors import (
    ACCOUNT_IDENTITY_SELECTOR,
    BODY_SELECTOR,
    CHAT_INPUT_SELECTOR,
    CHAT_MESSAGE_SELECTOR,
    CHAT_PANEL_SELECTOR,
    CHAT_SEND_SELECTOR,
    OPEN_CHAT_SELECTOR,
    OWN_CHAT_MESSAGE_SELECTOR,
    PRODUCT_IDENTITY_SELECTOR,
    SELLER_IDENTITY_SELECTOR,
)


@dataclass
class FakeNode:
    """
    保存离线 DOM 节点的文本、属性、可见性和可观测写操作。

    该对象不解析 CSS，也不访问浏览器；测试通过 selector 映射显式决定可见结构。
    """

    text: str = ""
    attributes: dict[str, str] = field(default_factory=dict)
    visible: bool = True
    enabled: bool = True
    filled_text: str = ""
    click_count: int = 0
    on_click: Callable[[], None] | None = None


class FakeLocator:
    """
    为测试实现聊天客户端使用的最小异步 Locator 接口。

    它只操作传入的 ``FakeNode`` 列表，不包含网络、计时器或浏览器副作用。
    """

    def __init__(self, nodes: list[FakeNode]) -> None:
        """
        保存当前 selector 对应的动态节点列表。

        参数为共享列表；无返回和异常；副作用仅为保存引用。
        """

        self._nodes = nodes

    def _single(self) -> FakeNode:
        """
        返回当前 locator 的唯一节点。

        无输入；返回节点；测试适配错误时抛出 ``AssertionError``；没有外部副作用。
        """

        if len(self._nodes) != 1:
            raise AssertionError("FakeLocator 操作需要唯一节点")
        return self._nodes[0]

    async def count(self) -> int:
        """
        返回 selector 当前节点数。

        无输入；返回整数；不抛出预期异常且没有副作用。
        """

        return len(self._nodes)

    def nth(self, index: int) -> "FakeLocator":
        """
        返回指定下标节点的 locator。

        参数为下标；返回新的离线 locator；越界返回空 locator；没有外部副作用。
        """

        return FakeLocator(self._nodes[index : index + 1])

    async def is_visible(self) -> bool:
        """
        返回唯一节点的可见性。

        无输入；返回布尔值；节点不唯一时抛出 ``AssertionError``；没有副作用。
        """

        return self._single().visible

    async def is_enabled(self) -> bool:
        """
        返回唯一节点的可用状态。

        无输入；返回布尔值；节点不唯一时抛出 ``AssertionError``；没有副作用。
        """

        return self._single().enabled

    async def inner_text(self, *, timeout: float | None = None) -> str:
        """
        返回唯一节点文本并忽略离线 timeout。

        参数为可选超时；返回文本；节点不唯一时抛出 ``AssertionError``；没有副作用。
        """

        del timeout
        return self._single().text

    async def get_attribute(self, name: str) -> str | None:
        """
        读取唯一节点的一个属性。

        参数为属性名；返回字符串或 ``None``；节点不唯一时抛出 ``AssertionError``。
        """

        return self._single().attributes.get(name)

    async def fill(self, value: str, *, timeout: float | None = None) -> None:
        """
        记录对唯一节点执行的输入文本。

        参数为文本和可选超时；无返回；节点不唯一时抛出 ``AssertionError``；副作用仅在内存。
        """

        del timeout
        self._single().filled_text = value

    async def click(self, *, timeout: float | None = None) -> None:
        """
        记录唯一节点点击并执行测试回调。

        参数为可选超时；无返回；节点不唯一时抛出 ``AssertionError``；副作用仅在内存。
        """

        del timeout
        node = self._single()
        node.click_count += 1
        if node.on_click is not None:
            node.on_click()


class FakePage:
    """
    按集中 selector 返回共享 FakeLocator 的离线 Page 实现。

    该对象没有 ``goto`` 或网络能力，因此测试无法意外访问真实闲鱼。
    """

    def __init__(self, url: str, nodes_by_selector: dict[str, list[FakeNode]]) -> None:
        """
        保存固定 URL 与 selector 映射。

        参数均来自测试；无返回和异常；副作用仅为保存内存引用。
        """

        self.url = url
        self.nodes_by_selector = nodes_by_selector

    def locator(self, selector: str) -> FakeLocator:
        """
        返回 selector 对应的动态离线 locator。

        参数为集中选择器；返回 locator；未知 selector 对应空列表；没有外部副作用。
        """

        return FakeLocator(self.nodes_by_selector.setdefault(selector, []))

    async def wait_for_timeout(self, timeout: float) -> None:
        """
        在离线测试中消费但不实际等待毫秒数。

        参数为等待毫秒；无返回和异常；不阻塞且没有副作用。
        """

        del timeout


class FakeAccountGuard:
    """
    记录聊天客户端是否在账号独占上下文内执行页面操作。

    它只维护内存计数，不访问 PostgreSQL 或登录态。
    """

    def __init__(self) -> None:
        """
        初始化进入次数和当前持有计数。

        无输入、返回和异常；副作用仅为创建内存计数。
        """

        self.entries = 0
        self.active = 0

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        """
        提供一次可观测的离线账号独占上下文。

        无输入；上下文内返回空值；无预期异常；进入和退出会更新内存计数。
        """

        self.entries += 1
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1


@dataclass
class FakeChatEnvironment:
    """
    汇总一个完整离线聊天页面和关键可观测节点。

    测试可修改消息列表或身份节点，以验证发送前并发检查和失败关闭。
    """

    page: FakePage
    input_node: FakeNode
    send_node: FakeNode
    open_node: FakeNode
    messages: list[FakeNode]
    own_messages: list[FakeNode]


def make_chat_environment(*, confirm_send: bool = True) -> FakeChatEnvironment:
    """
    创建绑定商品 ``item-100`` 的完整离线聊天 DOM。

    参数控制点击发送后是否追加本人消息；返回可变测试环境；不启动浏览器或访问网络。
    """

    input_node = FakeNode()
    send_node = FakeNode()
    open_node = FakeNode()
    messages = [
        FakeNode(
            text="还在，可以正常使用",
            attributes={
                "data-message-id": "msg-1",
                "data-direction": "seller",
                "data-timestamp": "2026-07-20T00:00:00Z",
            },
        )
    ]
    own_messages: list[FakeNode] = []
    nodes_by_selector = {
        BODY_SELECTOR: [FakeNode(text="闲鱼商品聊天")],
        PRODUCT_IDENTITY_SELECTOR: [FakeNode(attributes={"data-item-id": "item-100"})],
        SELLER_IDENTITY_SELECTOR: [FakeNode(attributes={"data-seller-id": "seller-200"})],
        ACCOUNT_IDENTITY_SELECTOR: [FakeNode(attributes={"data-account-id": "account-300"})],
        OPEN_CHAT_SELECTOR: [open_node],
        CHAT_PANEL_SELECTOR: [FakeNode()],
        CHAT_INPUT_SELECTOR: [input_node],
        CHAT_SEND_SELECTOR: [send_node],
        CHAT_MESSAGE_SELECTOR: messages,
        OWN_CHAT_MESSAGE_SELECTOR: own_messages,
    }

    def append_sent_message() -> None:
        """
        把输入框当前文本追加为唯一本人消息，模拟点击后 DOM 确认。

        无输入和返回；副作用只修改共享内存消息列表。
        """

        if not confirm_send:
            return
        sent = FakeNode(
            text=input_node.filled_text,
            attributes={
                "data-message-id": f"msg-{len(messages) + 1}",
                "data-direction": "self",
                "data-timestamp": "2026-07-20T00:00:01Z",
            },
        )
        messages.append(sent)
        own_messages.append(sent)

    send_node.on_click = append_sent_message
    page = FakePage("https://www.goofish.com/item?id=item-100", nodes_by_selector)
    return FakeChatEnvironment(page, input_node, send_node, open_node, messages, own_messages)


def make_client(environment: FakeChatEnvironment, guard: FakeAccountGuard) -> XianyuChatClient:
    """
    为固定身份绑定创建使用离线 Page 的聊天客户端。

    参数为测试环境与 guard；返回客户端；构造失败向上抛出；不读取页面或访问网络。
    """

    return XianyuChatClient(
        cast(Page, environment.page),
        ChatBinding("item-100", "seller-200", "account-300"),
        guard,
    )


def test_item_url_binding_requires_exact_https_item_identity() -> None:
    """
    验证 URL 必须是官方 HTTPS 商品页且唯一商品参数完全一致。

    无输入；断言失败抛出 ``AssertionError``；只执行纯 URL 解析。
    """

    assert item_url_matches_binding("https://www.goofish.com/item?id=item-100", "item-100")
    assert not item_url_matches_binding("http://www.goofish.com/item?id=item-100", "item-100")
    assert not item_url_matches_binding("https://www.goofish.com/item?id=item-101", "item-100")
    assert not item_url_matches_binding(
        "https://www.goofish.com/item?id=item-100&id=item-101", "item-100"
    )
    assert not item_url_matches_binding("https://example.com/item?id=item-100", "item-100")


def test_message_fingerprint_is_stable_after_text_normalization() -> None:
    """
    验证等价 Unicode 和空白文本产生相同指纹，方向变化产生不同指纹。

    无输入；断言失败抛出 ``AssertionError``；只执行纯摘要计算。
    """

    first = build_message_fingerprint(
        message_id="m1", direction="seller", text="可以  发货", timestamp="t1"
    )
    same = build_message_fingerprint(
        message_id="m1", direction="seller", text="可以 发货", timestamp="t1"
    )
    other = build_message_fingerprint(
        message_id="m1", direction="self", text="可以 发货", timestamp="t1"
    )
    assert first == same
    assert first != other


@pytest.mark.asyncio
async def test_send_requires_explicit_flag_and_never_touches_page_when_disabled() -> None:
    """
    验证自动发送开关不是严格 True 时不获取账号 guard、不输入也不点击。

    无输入；断言失败抛出 ``AssertionError``；副作用仅为读取离线节点计数。
    """

    environment = make_chat_environment()
    guard = FakeAccountGuard()
    client = make_client(environment, guard)
    draft = PolicyAllowedDraft("请问近期可以发货吗？", "policy-1")

    with pytest.raises(ChatSafetyError) as caught:
        await client.send_policy_allowed_draft(
            draft,
            expected_latest_fingerprint="0" * 64,
            auto_send_enabled=False,
        )

    assert caught.value.code == "auto_send_disabled"
    assert guard.entries == 0
    assert environment.input_node.filled_text == ""
    assert environment.send_node.click_count == 0


@pytest.mark.asyncio
async def test_send_holds_guard_rechecks_message_and_confirms_own_text() -> None:
    """
    验证发送全程持有 guard、复核最新消息，并等待新增本人同文消息。

    无输入；断言失败抛出 ``AssertionError``；所有页面副作用只发生在 Fake Node。
    """

    environment = make_chat_environment()
    guard = FakeAccountGuard()
    client = make_client(environment, guard)
    latest = await client.read_latest_message()
    evidence = await client.send_policy_allowed_draft(
        PolicyAllowedDraft("请问近期可以发货吗？", "policy-2"),
        expected_latest_fingerprint=latest.fingerprint,
        auto_send_enabled=True,
    )

    assert guard.entries == 2
    assert guard.active == 0
    assert environment.input_node.filled_text == "请问近期可以发货吗？"
    assert environment.send_node.click_count == 1
    assert environment.open_node.click_count == 0
    assert len(environment.own_messages) == 1
    assert evidence.source_item_id == "item-100"
    assert evidence.policy_decision_id == "policy-2"
    assert len(evidence.draft_sha256) == 64
    assert len(evidence.confirmed_message_fingerprint) == 64


@pytest.mark.asyncio
async def test_send_fails_closed_when_latest_message_changed() -> None:
    """
    验证读取草稿后卖家新增消息会阻止输入和发送。

    无输入；断言失败抛出 ``AssertionError``；只修改离线消息列表模拟并发。
    """

    environment = make_chat_environment()
    guard = FakeAccountGuard()
    client = make_client(environment, guard)
    latest = await client.read_latest_message()
    environment.messages.append(
        FakeNode(
            text="刚刚有人问了",
            attributes={"data-message-id": "msg-2", "data-direction": "seller"},
        )
    )

    with pytest.raises(ChatSafetyError) as caught:
        await client.send_policy_allowed_draft(
            PolicyAllowedDraft("请问还在吗？", "policy-3"),
            expected_latest_fingerprint=latest.fingerprint,
            auto_send_enabled=True,
        )

    assert caught.value.code == "conversation_changed_before_send"
    assert environment.input_node.filled_text == ""
    assert environment.send_node.click_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selector", "attribute", "wrong_value"),
    [
        (PRODUCT_IDENTITY_SELECTOR, "data-item-id", "item-other"),
        (SELLER_IDENTITY_SELECTOR, "data-seller-id", "seller-other"),
        (ACCOUNT_IDENTITY_SELECTOR, "data-account-id", "account-other"),
    ],
)
async def test_identity_mismatch_blocks_before_chat_write(
    selector: str, attribute: str, wrong_value: str
) -> None:
    """
    验证商品、卖家或当前账号任一不一致都会在聊天写入前失败关闭。

    参数由离线身份变体提供；断言失败抛出 ``AssertionError``；不访问网络。
    """

    environment = make_chat_environment()
    environment.page.nodes_by_selector[selector][0].attributes[attribute] = wrong_value
    guard = FakeAccountGuard()
    client = make_client(environment, guard)

    with pytest.raises(ChatSafetyError) as caught:
        await client.read_latest_message()

    assert caught.value.code == "chat_identity_mismatch"
    assert environment.input_node.filled_text == ""
    assert environment.send_node.click_count == 0


@pytest.mark.asyncio
async def test_ambiguous_visible_send_button_fails_closed() -> None:
    """
    验证两个可见发送按钮不会选择其一继续执行。

    无输入；断言失败抛出 ``AssertionError``；只修改离线 DOM 映射。
    """

    environment = make_chat_environment()
    guard = FakeAccountGuard()
    client = make_client(environment, guard)
    latest = await client.read_latest_message()
    environment.page.nodes_by_selector[CHAT_SEND_SELECTOR].append(FakeNode())

    with pytest.raises(ChatSafetyError) as caught:
        await client.send_policy_allowed_draft(
            PolicyAllowedDraft("请问还在吗？", "policy-4"),
            expected_latest_fingerprint=latest.fingerprint,
            auto_send_enabled=True,
        )

    assert caught.value.code == "ambiguous_chat_dom"
    assert environment.send_node.click_count == 0


@pytest.mark.asyncio
async def test_login_or_captcha_signal_reuses_risk_detection() -> None:
    """
    验证统一风险识别发现验证码文案时阻止任何聊天操作。

    无输入；断言失败抛出 ``AssertionError``；只改变 Fake body 文本。
    """

    environment = make_chat_environment()
    environment.page.nodes_by_selector[BODY_SELECTOR][0].text = "请完成验证码后继续"
    client = make_client(environment, FakeAccountGuard())

    with pytest.raises(ChatSafetyError) as caught:
        await client.read_latest_message()

    assert caught.value.code == "risk_or_login_blocked"
    assert environment.send_node.click_count == 0


@pytest.mark.asyncio
async def test_missing_send_confirmation_never_retries_click() -> None:
    """
    验证点击后未出现本人同文消息时返回不确定错误且绝不重复发送。

    无输入；断言失败抛出 ``AssertionError``；首次点击仅记录在离线节点。
    """

    environment = make_chat_environment(confirm_send=False)
    guard = FakeAccountGuard()
    client = make_client(environment, guard)
    latest = await client.read_latest_message()

    with pytest.raises(ChatSafetyError) as caught:
        await client.send_policy_allowed_draft(
            PolicyAllowedDraft("请问还在吗？", "policy-5"),
            expected_latest_fingerprint=latest.fingerprint,
            auto_send_enabled=True,
        )

    assert caught.value.code == "send_confirmation_missing"
    assert environment.send_node.click_count == 1
    assert environment.open_node.click_count == 0


@pytest.mark.asyncio
async def test_open_conversation_clicks_only_the_unique_chat_entry() -> None:
    """
    验证打开会话只点击集中定义的聊天入口并返回已有卖家消息指纹。

    无输入；断言失败抛出 ``AssertionError``；所有点击只记录在 Fake Node。
    """

    environment = make_chat_environment()
    guard = FakeAccountGuard()
    client = make_client(environment, guard)
    latest = await client.open_conversation()

    assert latest.direction == "seller"
    assert environment.open_node.click_count == 1
    assert environment.send_node.click_count == 0
    assert guard.entries == 1
