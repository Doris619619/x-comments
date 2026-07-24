"""
本文件离线验证闲鱼聊天页面适配层的身份锁定、消息指纹和失败关闭发送边界。

它属于 crawler 单元测试，只使用内存 Fake Page/Locator 和 Fake AccountAccessGuard，绝不
启动 Playwright 浏览器、不访问网络、不读取本地登录态，也不触发真实闲鱼聊天或交易。
"""

import hashlib
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
    chat_url_matches_binding,
    discover_chat_binding,
    item_url_matches_binding,
)
from app.crawler.chat_selectors import (
    BODY_SELECTOR,
    CHAT_ENTRY_WAIT_MILLISECONDS,
    CHAT_INPUT_SELECTOR,
    CHAT_MESSAGE_LIST_SELECTOR,
    CHAT_MESSAGE_SELECTOR,
    CHAT_PANEL_SELECTOR,
    CHAT_READY_WAIT_MILLISECONDS,
    CHAT_SEND_SELECTOR,
    OPEN_CHAT_SELECTOR,
    OWN_CHAT_MESSAGE_SELECTOR,
)
from app.services.xianyu_account_guard import AccountAccessGuard


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
    pressed_keys: list[str] = field(default_factory=list)
    on_click: Callable[[], None] | None = None
    on_press: Callable[[str], None] | None = None


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

    async def evaluate(self, expression: str) -> str:
        """
        返回离线节点声明的 flex 排列方向。

        参数为生产代码的计算样式表达式；返回测试属性；不执行 JavaScript。
        """

        del expression
        return self._single().attributes.get("data-flex-direction", "column")

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

    async def press(self, key: str, *, timeout: float | None = None) -> None:
        """
        记录唯一输入节点的按键，并执行测试回调。

        参数为按键和可选超时；无返回；节点不唯一时抛出 ``AssertionError``；副作用仅在内存。
        """

        del timeout
        node = self._single()
        node.pressed_keys.append(key)
        if node.on_press is not None:
            node.on_press(key)


class FakeContext:
    """
    提供聊天客户端所需的最小浏览器上下文。

    它保存页面列表和账号 Cookie，只服务离线测试，不持有真实登录态。
    """

    def __init__(self, account_id: str) -> None:
        """保存离线账号 ID，并初始化空页面列表。"""

        self.account_id = account_id
        self.pages: list[FakePage] = []

    async def cookies(self, *urls: str) -> list[dict[str, str]]:
        """返回一个离线 ``tracknick`` Cookie；URL 参数仅用于兼容生产接口。"""

        del urls
        return [{"name": "tracknick", "value": self.account_id}]


class FakePage:
    """
    按集中 selector 返回共享 FakeLocator 的离线 Page 实现。

    该对象没有 ``goto`` 或网络能力，因此测试无法意外访问真实闲鱼。
    """

    def __init__(
        self,
        url: str,
        nodes_by_selector: dict[str, list[FakeNode]],
        *,
        account_id: str = "account-300",
    ) -> None:
        """
        保存固定 URL 与 selector 映射。

        参数均来自测试；无返回和异常；副作用仅为保存内存引用。
        """

        self.url = url
        self.nodes_by_selector = nodes_by_selector
        self.context = FakeContext(account_id)
        self.context.pages.append(self)
        self.wait_for_selector_calls: list[tuple[str, str, float]] = []

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

    async def wait_for_selector(
        self,
        selector: str,
        *,
        state: str,
        timeout: float,
    ) -> FakeLocator:
        """
        记录延迟入口等待并返回对应离线 locator。

        输入集中选择器、状态与毫秒时限；返回内存 locator；没有网络或真实等待副作用。
        """

        self.wait_for_selector_calls.append((selector, state, timeout))
        return self.locator(selector)

    async def wait_for_load_state(
        self,
        state: str,
        *,
        timeout: float | None = None,
    ) -> None:
        """消费离线页面加载等待参数，不阻塞且不访问网络。"""

        del state, timeout


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

    参数控制按 Enter 后是否追加本人消息；返回可变测试环境；不启动浏览器或访问网络。
    """

    input_node = FakeNode()
    send_node = FakeNode()
    open_node = FakeNode()
    messages = [
        FakeNode(
            text="还在，可以正常使用",
            attributes={
                "data-message-id": "msg-1",
                "class": "msg-text-left--fixture",
                "data-timestamp": "2026-07-20T00:00:00Z",
            },
        )
    ]
    own_messages: list[FakeNode] = []
    nodes_by_selector = {
        BODY_SELECTOR: [FakeNode(text="闲鱼商品聊天")],
        OPEN_CHAT_SELECTOR: [open_node],
        CHAT_PANEL_SELECTOR: [FakeNode()],
        CHAT_MESSAGE_LIST_SELECTOR: [
            FakeNode(attributes={"data-flex-direction": "column-reverse"})
        ],
        CHAT_INPUT_SELECTOR: [input_node],
        CHAT_SEND_SELECTOR: [send_node],
        CHAT_MESSAGE_SELECTOR: messages,
        OWN_CHAT_MESSAGE_SELECTOR: own_messages,
    }

    def append_sent_message() -> None:
        """
        把输入框当前文本追加为唯一本人消息，模拟 Enter 提交后的 DOM 确认。

        无输入和返回；副作用只修改共享内存消息列表。
        """

        if not confirm_send:
            return
        sent = FakeNode(
            text=input_node.filled_text,
            attributes={
                "data-message-id": f"msg-{len(messages) + 1}",
                "class": "msg-text-right--fixture",
                "data-timestamp": "2026-07-20T00:00:01Z",
            },
        )
        messages.insert(0, sent)
        own_messages.insert(0, sent)

    def submit_by_enter(key: str) -> None:
        """只在生产代码约定的 Enter 键出现时模拟一次页面发送。"""

        if key == "Enter":
            append_sent_message()

    input_node.on_press = submit_by_enter
    page = FakePage("https://www.goofish.com/item?id=item-100", nodes_by_selector)
    open_node.text = "聊一聊"
    open_node.attributes["href"] = (
        "https://www.goofish.com/im?itemId=item-100&peerUserId=seller-200"
    )

    def open_chat() -> None:
        """把离线商品页切换到严格绑定的聊天 URL。"""

        page.url = open_node.attributes["href"]

    open_node.on_click = open_chat
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


def test_chat_url_binding_requires_exact_item_and_seller() -> None:
    """
    验证聊天 URL 必须同时绑定唯一商品与卖家参数。

    无输入；断言失败抛出 ``AssertionError``；只执行纯 URL 解析。
    """

    valid = "https://www.goofish.com/im?itemId=item-100&peerUserId=seller-200"
    assert chat_url_matches_binding(valid, "item-100", "seller-200")
    assert not chat_url_matches_binding(valid, "item-other", "seller-200")
    assert not chat_url_matches_binding(valid, "item-100", "seller-other")


@pytest.mark.asyncio
async def test_discovers_seller_and_accepts_hashed_account_binding() -> None:
    """
    验证首次进入商品页时从聊天 URL 锁定卖家，并以账号 Cookie 指纹完成绑定。

    无输入；只读取离线 DOM 和 Cookie，不触发点击、网络或真实聊天。
    """

    environment = make_chat_environment()
    expected_fingerprint = hashlib.sha256(b"account-300").hexdigest()
    binding = await discover_chat_binding(
        cast(Page, environment.page),
        source_item_id="item-100",
        expected_account_id=expected_fingerprint,
        account_guard=cast(AccountAccessGuard, FakeAccountGuard()),
    )

    assert binding.source_item_id == "item-100"
    assert binding.seller_id == "seller-200"
    assert binding.account_id == expected_fingerprint
    assert environment.open_node.click_count == 0
    assert environment.page.wait_for_selector_calls == [
        (OPEN_CHAT_SELECTOR, "visible", CHAT_ENTRY_WAIT_MILLISECONDS)
    ]


def test_open_chat_selector_excludes_generic_sidebar_message_link() -> None:
    """
    验证聊天入口同时限定主商品 want 控件和完整 IM 身份参数。

    无输入；断言失败抛出 AssertionError；只检查集中选择器字符串，不访问页面或网络。
    """

    assert "want--" in OPEN_CHAT_SELECTOR
    assert "itemId=" in OPEN_CHAT_SELECTOR
    assert "peerUserId=" in OPEN_CHAT_SELECTOR


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
    assert environment.input_node.pressed_keys == []
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
    assert environment.input_node.pressed_keys == ["Enter"]
    assert environment.send_node.click_count == 0
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
    environment.messages.insert(
        0,
        FakeNode(
            text="刚刚有人问了",
            attributes={"data-message-id": "msg-2", "class": "msg-text-left--fixture"},
        ),
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
@pytest.mark.parametrize("mismatch", ["item", "seller", "account"])
async def test_identity_mismatch_blocks_before_chat_write(mismatch: str) -> None:
    """
    验证商品、卖家或当前账号任一不一致都会在聊天写入前失败关闭。

    参数指定要破坏的身份边界；断言失败抛出 ``AssertionError``；不访问网络。
    """

    environment = make_chat_environment()
    if mismatch == "item":
        environment.page.url = "https://www.goofish.com/item?id=item-other"
    elif mismatch == "seller":
        environment.open_node.attributes["href"] = (
            "https://www.goofish.com/im?itemId=item-100&peerUserId=seller-other"
        )
    else:
        environment.page.context.account_id = "account-other"
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
async def test_missing_send_confirmation_never_retries_submit() -> None:
    """
    验证 Enter 提交后未出现本人同文消息时返回不确定错误且绝不重复发送。

    无输入；断言失败抛出 ``AssertionError``；首次按键仅记录在离线节点。
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
    assert environment.input_node.pressed_keys == ["Enter"]
    assert environment.send_node.click_count == 0
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
    assert environment.page.wait_for_selector_calls == [
        (CHAT_PANEL_SELECTOR, "visible", CHAT_READY_WAIT_MILLISECONDS),
        (CHAT_MESSAGE_LIST_SELECTOR, "visible", CHAT_READY_WAIT_MILLISECONDS),
        (CHAT_INPUT_SELECTOR, "visible", CHAT_READY_WAIT_MILLISECONDS),
        (CHAT_SEND_SELECTOR, "visible", CHAT_READY_WAIT_MILLISECONDS),
    ]


@pytest.mark.asyncio
async def test_reads_all_visible_messages_after_baseline_in_order() -> None:
    """
    验证卖家连续发送两条消息时会完整按顺序返回，而不是只读取最后一条。

    无输入；副作用仅修改离线消息数组，不访问真实聊天页面。
    """

    environment = make_chat_environment()
    client = make_client(environment, FakeAccountGuard())
    baseline = await client.read_latest_message()
    # column-reverse 页面以“最新在前”的 DOM 顺序保存节点。
    environment.messages[0:0] = [
        FakeNode(
            text="明天可以发货",
            attributes={"data-message-id": "msg-3", "class": "msg-text-left--fixture"},
        ),
        FakeNode(
            text="还在",
            attributes={"data-message-id": "msg-2", "class": "msg-text-left--fixture"},
        ),
    ]

    messages = await client.read_messages_after(baseline.fingerprint)

    assert [message.text for message in messages] == ["还在", "明天可以发货"]
    assert all(message.direction == "seller" for message in messages)
