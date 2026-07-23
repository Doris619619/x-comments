"""
本文件实现单个已知闲鱼商品的保守聊天页面适配层。

它属于 crawler 模块：只校验页面与账号身份、读取最新消息、打开聊天控件，并发送已经由
上层策略批准的草稿。所有写操作都在账号级 ``AccountAccessGuard`` 内执行，并在任何身份、
页面结构、登录态、风控或消息并发不确定时失败关闭。

本文件不生成 AI 文案、不判断采购结果、不访问数据库，也绝不点击购买、付款、地址或订单
确认控件。选择器统一定义在 ``chat_selectors``，实际发送仍需调用方显式开启自动发送。
"""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from playwright.async_api import Locator, Page, Response

from app.crawler.chat_selectors import (
    ACCOUNT_IDENTITY_COOKIE_NAME,
    BODY_SELECTOR,
    CHAT_INPUT_SELECTOR,
    CHAT_MESSAGE_LIST_SELECTOR,
    CHAT_MESSAGE_SELECTOR,
    CHAT_PANEL_SELECTOR,
    CHAT_SEND_SELECTOR,
    MESSAGE_DIRECTION_ATTRIBUTES,
    MESSAGE_ID_ATTRIBUTES,
    MESSAGE_TIMESTAMP_ATTRIBUTES,
    OPEN_CHAT_SELECTOR,
    OWN_CHAT_MESSAGE_SELECTOR,
)
from app.crawler.risk_control import RiskControlBlocked, detect_risk
from app.services.xianyu_account_guard import AccountAccessGuard

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SELF_DIRECTIONS = {"self", "outgoing", "buyer", "mine"}
SELLER_DIRECTIONS = {"seller", "incoming", "other"}
MAX_MESSAGE_NODES = 500
SEND_CONFIRMATION_ATTEMPTS = 20
SEND_CONFIRMATION_DELAY_MS = 100


class ChatSafetyError(RiskControlBlocked):
    """
    表示聊天适配层因稳定安全边界而停止。

    调用方可读取 ``code`` 做状态映射；异常消息只描述安全分类，不包含登录态或凭据。
    """

    def __init__(self, code: str, message: str) -> None:
        """
        保存稳定错误码和可诊断消息。

        参数为非敏感错误码与消息；无返回；副作用仅为初始化异常对象。
        """

        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ChatBinding:
    """
    固定一次聊天允许操作的商品、卖家与当前买家账号身份。

    三个标识必须由可信任务快照提供；对象不负责查询或推断任何身份。
    """

    source_item_id: str
    seller_id: str
    account_id: str

    def __post_init__(self) -> None:
        """
        在页面访问前验证三个绑定标识是非空稳定字符串。

        无显式返回；非法标识抛出 ``ChatSafetyError``；不访问页面且没有外部副作用。
        """

        for field_name, value in (
            ("source_item_id", self.source_item_id),
            ("seller_id", self.seller_id),
            ("account_id", self.account_id),
        ):
            if not IDENTIFIER_PATTERN.fullmatch(value):
                raise ChatSafetyError(
                    "invalid_chat_binding",
                    f"聊天绑定字段 {field_name} 不是可确认的稳定标识",
                )


@dataclass(frozen=True, slots=True)
class PolicyAllowedDraft:
    """
    表示已经由上层确定性策略放行的单条聊天草稿。

    适配层只校验文本基本边界和策略决策 ID，不重新执行 LLM 或业务审核。
    """

    text: str
    policy_decision_id: str

    def __post_init__(self) -> None:
        """
        验证草稿文本与策略决策 ID，避免空消息或无审计来源的发送。

        无显式返回；边界不合法时抛出 ``ChatSafetyError``；不访问页面。
        """

        normalized = normalize_chat_text(self.text)
        if not normalized or len(normalized) > 500:
            raise ChatSafetyError("invalid_allowed_draft", "已放行草稿必须为 1 至 500 个字符")
        if not IDENTIFIER_PATTERN.fullmatch(self.policy_decision_id):
            raise ChatSafetyError("invalid_policy_decision", "草稿缺少稳定策略决策标识")


@dataclass(frozen=True, slots=True)
class ChatMessageSnapshot:
    """
    保存聊天窗口最新一条可见消息的稳定只读快照。

    空会话使用 ``direction=none`` 和确定性指纹，不把 DOM 节点或登录态带出适配层。
    """

    message_id: str | None
    direction: str
    text: str
    timestamp: str | None
    fingerprint: str


@dataclass(frozen=True, slots=True)
class SendEvidence:
    """
    表示一次点击发送后由页面中本人消息文本确认的最小证据。

    证据只含绑定 ID、策略决策 ID、草稿摘要与确认消息指纹，不保存账号凭据。
    """

    source_item_id: str
    seller_id: str
    account_id: str
    policy_decision_id: str
    draft_sha256: str
    confirmed_message_fingerprint: str


def normalize_chat_text(value: str) -> str:
    """
    将消息文本规范化为适合比较和指纹计算的稳定形式。

    参数为页面或草稿文本；返回 Unicode NFKC 且空白折叠后的字符串；无异常和副作用。
    """

    return " ".join(unicodedata.normalize("NFKC", value).split())


def item_url_matches_binding(url: str, source_item_id: str) -> bool:
    """
    严格确认当前 URL 是绑定商品的闲鱼详情页。

    参数为页面 URL 和已知商品 ID；仅官方主机、``/item`` 路径和唯一 ``id`` 参数完全一致
    时返回 ``True``；解析失败返回 ``False``，不访问网络。
    """

    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    item_ids = parse_qs(parsed.query, keep_blank_values=True).get("id", [])
    return (
        parsed.scheme == "https"
        and host in {"goofish.com", "www.goofish.com"}
        and parsed.path.rstrip("/") == "/item"
        and item_ids == [source_item_id]
    )


def chat_url_matches_binding(
    url: str,
    source_item_id: str,
    seller_id: str,
) -> bool:
    """
    严格确认当前 URL 是绑定商品和卖家的闲鱼聊天页。

    参数为页面 URL、商品 ID 和卖家 ID；只接受官方 HTTPS ``/im`` 页面且 ``itemId``、
    ``peerUserId`` 各自唯一并完全一致。解析失败返回 ``False``，没有网络副作用。
    """

    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() in {"goofish.com", "www.goofish.com"}
        and parsed.path.rstrip("/") == "/im"
        and query.get("itemId") == [source_item_id]
        and query.get("peerUserId") == [seller_id]
    )


def _parse_chat_entry_href(
    page_url: str,
    href: str,
    source_item_id: str,
) -> str:
    """
    从商品页聊天入口提取唯一卖家 ID。

    输入当前页、入口 href 和商品 ID；返回 ``peerUserId``。URL 非官方、商品不一致或卖家
    参数缺失/重复时抛出安全异常；只解析字符串。
    """

    parsed = urlparse(urljoin(page_url, href))
    query = parse_qs(parsed.query, keep_blank_values=True)
    seller_ids = query.get("peerUserId", [])
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() not in {"goofish.com", "www.goofish.com"}
        or parsed.path.rstrip("/") != "/im"
        or query.get("itemId") != [source_item_id]
        or len(seller_ids) != 1
        or not IDENTIFIER_PATTERN.fullmatch(seller_ids[0])
    ):
        raise ChatSafetyError("chat_entry_identity_invalid", "聊天入口身份参数无法安全确认")
    return seller_ids[0]


def build_message_fingerprint(
    *, message_id: str | None, direction: str, text: str, timestamp: str | None
) -> str:
    """
    根据消息稳定字段生成 SHA-256 指纹。

    参数为可选消息 ID、方向、规范化文本和可选时间；返回小写十六进制摘要；不读取页面。
    """

    payload = json.dumps(
        {
            "direction": direction,
            "message_id": message_id,
            "text": normalize_chat_text(text),
            "timestamp": timestamp,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _unique_visible_locator(page: Page, selector: str, label: str) -> Locator:
    """
    从一个集中选择器中返回唯一可见元素。

    参数为页面、选择器和非敏感标签；找不到或出现多个可见元素时抛出 ``ChatSafetyError``；
    只读取 DOM 可见性，不点击或输入。
    """

    locator = page.locator(selector)
    visible: list[Locator] = []
    for index in range(await locator.count()):
        node = locator.nth(index)
        if await node.is_visible():
            visible.append(node)
            if len(visible) > 1:
                break
    if len(visible) != 1:
        raise ChatSafetyError(
            "ambiguous_chat_dom",
            f"{label} 必须且只能匹配一个可见元素",
        )
    return visible[0]


async def _read_unique_attribute(
    locator: Locator, attribute_names: tuple[str, ...], label: str
) -> str:
    """
    从唯一元素的白名单属性中读取唯一非空身份值。

    属性缺失或互相冲突时抛出 ``ChatSafetyError``；不使用页面文本猜测身份且没有写操作。
    """

    values = {
        value.strip()
        for name in attribute_names
        if (value := await locator.get_attribute(name)) is not None and value.strip()
    }
    if len(values) != 1:
        raise ChatSafetyError(
            "identity_not_confirmed",
            f"{label} 缺少唯一可确认的身份属性",
        )
    return next(iter(values))


async def _assert_account_cookie_identity(page: Page, expected_account_id: str) -> None:
    """
    用 ``tracknick`` Cookie 的原值或 SHA-256 指纹确认当前登录买家账号。

    生产配置推荐保存 64 位摘要而非昵称原值。函数只读取当前官方域 Cookie，不返回或记录
    原值；缺失、重复或不匹配均失败关闭。
    """

    cookies = await page.context.cookies("https://www.goofish.com")
    values = {
        unquote(str(cookie.get("value") or "")).strip()
        for cookie in cookies
        if str(cookie.get("name") or "").casefold() == ACCOUNT_IDENTITY_COOKIE_NAME.casefold()
        and str(cookie.get("value") or "").strip()
    }
    if len(values) != 1:
        raise ChatSafetyError("account_identity_not_confirmed", "无法唯一确认当前闲鱼账号")
    actual = next(iter(values))
    actual_fingerprint = hashlib.sha256(actual.encode("utf-8")).hexdigest()
    if expected_account_id not in {actual, actual_fingerprint}:
        raise ChatSafetyError("chat_identity_mismatch", "当前闲鱼账号与配置绑定不一致")


async def _read_optional_attribute(
    locator: Locator, attribute_names: tuple[str, ...]
) -> str | None:
    """
    从消息节点读取至多一个非空属性值。

    参数为消息节点和属性白名单；没有值返回 ``None``，冲突时抛出 ``ChatSafetyError``；
    无页面写入副作用。
    """

    values = {
        value.strip()
        for name in attribute_names
        if (value := await locator.get_attribute(name)) is not None and value.strip()
    }
    if len(values) > 1:
        raise ChatSafetyError("message_identity_ambiguous", "消息稳定属性存在冲突")
    return next(iter(values), None)


def _normalize_direction(value: str | None) -> str:
    """
    将页面消息方向映射为 ``self`` 或 ``seller``。

    输入页面属性；无法确定方向时抛出 ``ChatSafetyError``；函数没有外部副作用。
    """

    normalized = (value or "").strip().casefold()
    if normalized in SELF_DIRECTIONS or "msg-text-right--" in normalized:
        return "self"
    if normalized in SELLER_DIRECTIONS or "msg-text-left--" in normalized:
        return "seller"
    raise ChatSafetyError("message_direction_not_confirmed", "无法确认最新消息发送方向")


async def _snapshot_message(
    locator: Locator, forced_direction: str | None = None
) -> ChatMessageSnapshot:
    """
    将一个可见消息节点转换为稳定快照。

    参数为消息节点和可选可信方向；空文本或方向不明时抛出 ``ChatSafetyError``；只读取 DOM。
    """

    text = normalize_chat_text(await locator.inner_text(timeout=2_000))
    if not text:
        raise ChatSafetyError("empty_chat_message", "最新可见消息文本为空")
    message_id = await _read_optional_attribute(locator, MESSAGE_ID_ATTRIBUTES)
    timestamp = await _read_optional_attribute(locator, MESSAGE_TIMESTAMP_ATTRIBUTES)
    direction = forced_direction or _normalize_direction(
        await _read_optional_attribute(locator, MESSAGE_DIRECTION_ATTRIBUTES)
    )
    fingerprint = build_message_fingerprint(
        message_id=message_id,
        direction=direction,
        text=text,
        timestamp=timestamp,
    )
    return ChatMessageSnapshot(message_id, direction, text, timestamp, fingerprint)


def _empty_conversation_snapshot() -> ChatMessageSnapshot:
    """
    返回无可见消息时的确定性空会话快照。

    无输入和异常；返回固定指纹；不访问页面且没有副作用。
    """

    fingerprint = build_message_fingerprint(
        message_id=None,
        direction="none",
        text="",
        timestamp=None,
    )
    return ChatMessageSnapshot(None, "none", "", None, fingerprint)


async def discover_chat_binding(
    page: Page,
    *,
    source_item_id: str,
    expected_account_id: str,
    account_guard: AccountAccessGuard,
) -> ChatBinding:
    """
    在已验证商品详情页上发现卖家 ID，并同时锁定商品与当前账号身份。

    输入页面、商城订单绑定商品 ID、配置中的预期账号 ID 和账号 guard；返回三方不可变
    ``ChatBinding``。任一 DOM 身份缺失、歧义、URL 不匹配、登录或风控信号都会失败关闭；
    只读页面，不打开聊天、不发送消息，也不接触购买、付款或地址控件。
    """

    if not IDENTIFIER_PATTERN.fullmatch(source_item_id):
        raise ChatSafetyError("invalid_source_item_id", "商品 ID 不是可确认的稳定标识")
    if not IDENTIFIER_PATTERN.fullmatch(expected_account_id):
        raise ChatSafetyError("invalid_expected_account", "配置账号 ID 不是可确认的稳定标识")
    async with account_guard.hold():
        body = await _unique_visible_locator(page, BODY_SELECTOR, "页面主体")
        visible_text = await body.inner_text(timeout=5_000)
        reason = detect_risk(page.url, visible_text)
        if reason:
            raise ChatSafetyError("risk_or_login_blocked", reason)
        if not item_url_matches_binding(page.url, source_item_id):
            raise ChatSafetyError("item_url_mismatch", "当前 URL 与绑定闲鱼商品不一致")

        await _assert_account_cookie_identity(page, expected_account_id)
        entry = await _unique_visible_locator(page, OPEN_CHAT_SELECTOR, "聊天入口")
        entry_text = normalize_chat_text(await entry.inner_text(timeout=2_000))
        if entry_text != "聊一聊":
            raise ChatSafetyError("chat_entry_label_changed", "聊天入口文案与标定结果不一致")
        href = await entry.get_attribute("href")
        if not href:
            raise ChatSafetyError("chat_entry_identity_invalid", "聊天入口缺少身份 URL")
        seller_id = _parse_chat_entry_href(page.url, href, source_item_id)
        return ChatBinding(
            source_item_id=source_item_id,
            seller_id=seller_id,
            account_id=expected_account_id,
        )


class XianyuChatClient:
    """
    在一个已打开页面上执行身份锁定、只读消息检查和受控聊天发送。

    实例永久绑定一个商品、卖家和账号；所有公开页面操作均持有调用方提供的账号 guard。
    """

    def __init__(
        self,
        page: Page,
        binding: ChatBinding,
        account_guard: AccountAccessGuard,
    ) -> None:
        """
        保存页面、不可变身份绑定和账号访问 guard。

        参数缺少 guard 时抛出 ``ChatSafetyError``；不读取页面、不开启浏览器且不获取锁。
        """

        if account_guard is None:
            raise ChatSafetyError("account_guard_required", "聊天发送必须持有账号访问 guard")
        self._page = page
        self._binding = binding
        self._account_guard = account_guard
        self._blocked_http_status: int | None = None

        def observe_status(response: Response) -> None:
            """
            记录聊天上下文出现的首个 403/429。

            输入 Playwright 响应；无返回；只保存粗粒度状态码，不读取或记录响应正文。
            """

            if response.status in {403, 429} and self._blocked_http_status is None:
                self._blocked_http_status = response.status

        # 生产 Playwright BrowserContext 提供 on；离线 FakeContext 不注册网络监听器。
        if hasattr(self._page.context, "on"):
            self._page.context.on("response", observe_status)

    async def open_conversation(self) -> ChatMessageSnapshot:
        """
        在严格身份确认后点击唯一聊天入口并返回当前最新消息快照。

        无输入；返回只读消息快照；登录、风控、身份或 DOM 不确定时抛出 ``ChatSafetyError``。
        副作用仅为点击聊天入口，不点击任何交易控件。
        """

        async with self._account_guard.hold():
            await self._assert_bound_identity()
            trigger = await _unique_visible_locator(self._page, OPEN_CHAT_SELECTOR, "聊天入口")
            if not await trigger.is_enabled():
                raise ChatSafetyError("chat_entry_disabled", "聊天入口当前不可用")
            previous_pages = set(self._page.context.pages)
            await trigger.click(timeout=5_000)
            candidates: list[Page] = []
            for _ in range(20):
                new_pages = [
                    page for page in self._page.context.pages if page not in previous_pages
                ]
                candidates = [
                    page
                    for page in [self._page, *new_pages]
                    if chat_url_matches_binding(
                        page.url,
                        self._binding.source_item_id,
                        self._binding.seller_id,
                    )
                ]
                if candidates:
                    break
                await self._page.wait_for_timeout(250)
            if len(candidates) != 1:
                raise ChatSafetyError(
                    "chat_navigation_not_confirmed",
                    "点击入口后无法唯一确认绑定聊天页",
                )
            self._page = candidates[0]
            await self._page.wait_for_load_state("domcontentloaded", timeout=10_000)
            await self._assert_bound_identity()
            await self._assert_chat_ready()
            return await self._read_latest_message_unlocked()

    async def read_latest_message(self) -> ChatMessageSnapshot:
        """
        在账号 guard 内读取绑定会话的最新可见消息。

        无输入；返回稳定快照；身份、登录、风控或聊天 DOM 不确定时失败关闭；没有写操作。
        """

        async with self._account_guard.hold():
            await self._assert_bound_identity()
            await self._assert_chat_ready()
            return await self._read_latest_message_unlocked()

    async def read_messages_after(
        self,
        baseline_fingerprint: str,
    ) -> list[ChatMessageSnapshot]:
        """
        按页面顺序读取任务基线之后的全部可见消息。

        输入最近一次已持久化页面消息指纹；返回所有新增快照。基线从可见历史消失、格式
        无效或消息方向不明时失败关闭，避免漏掉卖家连续回复。
        """

        if not FINGERPRINT_PATTERN.fullmatch(baseline_fingerprint):
            raise ChatSafetyError("invalid_message_baseline", "聊天消息基线指纹无效")
        async with self._account_guard.hold():
            await self._assert_bound_identity()
            await self._assert_chat_ready()
            snapshots = await self._read_visible_messages_unlocked()
            if not snapshots:
                return []
            empty_fingerprint = _empty_conversation_snapshot().fingerprint
            if baseline_fingerprint == empty_fingerprint:
                return snapshots
            baseline_indexes = [
                index
                for index, snapshot in enumerate(snapshots)
                if snapshot.fingerprint == baseline_fingerprint
            ]
            if not baseline_indexes:
                raise ChatSafetyError(
                    "chat_baseline_not_visible",
                    "消息基线已不在可见历史中，禁止猜测缺失回复",
                )
            if len(baseline_indexes) != 1:
                raise ChatSafetyError(
                    "chat_baseline_ambiguous",
                    "可见历史中存在重复基线，禁止猜测消息边界",
                )
            return snapshots[baseline_indexes[0] + 1 :]

    async def send_policy_allowed_draft(
        self,
        draft: PolicyAllowedDraft,
        *,
        expected_latest_fingerprint: str,
        auto_send_enabled: bool,
    ) -> SendEvidence:
        """
        发送一条已放行草稿，并等待页面出现本人同文消息作为确认。

        调用方必须显式传入 ``auto_send_enabled=True`` 和读取阶段的最新消息指纹。方法在账号
        guard 内再次确认身份、风险和消息未变化，任何不确定均抛出 ``ChatSafetyError``；
        唯一写操作是填入聊天输入框并单击聊天发送按钮，且不会自动重试。
        """

        if auto_send_enabled is not True:
            raise ChatSafetyError("auto_send_disabled", "自动发送开关未显式开启")
        if not FINGERPRINT_PATTERN.fullmatch(expected_latest_fingerprint):
            raise ChatSafetyError("invalid_expected_fingerprint", "缺少有效的最新消息指纹")

        async with self._account_guard.hold():
            await self._assert_bound_identity()
            chat_input, _ = await self._assert_chat_ready()
            latest = await self._read_latest_message_unlocked()
            self._assert_unchanged(latest, expected_latest_fingerprint)
            own_count_before = await self._count_matching_own_messages(draft.text)

            await chat_input.fill(draft.text, timeout=5_000)
            await self._assert_bound_identity()
            latest_before_click = await self._read_latest_message_unlocked()
            self._assert_unchanged(latest_before_click, expected_latest_fingerprint)

            _, send_button = await self._assert_chat_ready()
            if not await send_button.is_enabled():
                raise ChatSafetyError("chat_send_disabled", "聊天发送按钮当前不可用")
            await send_button.click(timeout=5_000)
            confirmation = await self._wait_for_own_confirmation(draft.text, own_count_before)
            return SendEvidence(
                source_item_id=self._binding.source_item_id,
                seller_id=self._binding.seller_id,
                account_id=self._binding.account_id,
                policy_decision_id=draft.policy_decision_id,
                draft_sha256=hashlib.sha256(
                    normalize_chat_text(draft.text).encode("utf-8")
                ).hexdigest(),
                confirmed_message_fingerprint=confirmation.fingerprint,
            )

    async def _assert_safe(self) -> None:
        """
        使用项目统一 ``detect_risk`` 检查登录、验证码和风控可见信号。

        无输入和返回；风险信号或 body 不唯一时抛出 ``ChatSafetyError``；只读取页面。
        """

        if self._blocked_http_status in {403, 429}:
            raise ChatSafetyError("http_risk_blocked", "闲鱼聊天上下文返回访问控制状态")
        body = await _unique_visible_locator(self._page, BODY_SELECTOR, "页面主体")
        visible_text = await body.inner_text(timeout=5_000)
        reason = detect_risk(self._page.url, visible_text)
        if reason:
            raise ChatSafetyError("risk_or_login_blocked", reason)

    async def _assert_bound_identity(self) -> None:
        """
        同时确认 URL、商品、卖家和当前账号均与不可变绑定一致。

        无输入和返回；任一身份缺失、冲突或不一致时抛出 ``ChatSafetyError``；只读页面。
        """

        await self._assert_safe()
        await _assert_account_cookie_identity(self._page, self._binding.account_id)
        if item_url_matches_binding(self._page.url, self._binding.source_item_id):
            entry = await _unique_visible_locator(self._page, OPEN_CHAT_SELECTOR, "聊天入口")
            href = await entry.get_attribute("href")
            seller_id = (
                _parse_chat_entry_href(
                    self._page.url,
                    href,
                    self._binding.source_item_id,
                )
                if href
                else None
            )
            if seller_id != self._binding.seller_id:
                raise ChatSafetyError("chat_identity_mismatch", "卖家身份与任务绑定不一致")
            return
        if not chat_url_matches_binding(
            self._page.url,
            self._binding.source_item_id,
            self._binding.seller_id,
        ):
            raise ChatSafetyError("chat_identity_mismatch", "聊天页商品或卖家身份不一致")

    async def _assert_chat_ready(self) -> tuple[Locator, Locator]:
        """
        确认聊天面板、输入框和发送按钮均只有一个可见元素。

        无输入；返回输入框与发送按钮；DOM 缺失或歧义时抛出 ``ChatSafetyError``；只读 DOM。
        """

        await _unique_visible_locator(self._page, CHAT_PANEL_SELECTOR, "聊天面板")
        chat_input = await _unique_visible_locator(self._page, CHAT_INPUT_SELECTOR, "聊天输入框")
        send_button = await _unique_visible_locator(self._page, CHAT_SEND_SELECTOR, "聊天发送按钮")
        return chat_input, send_button

    async def _read_latest_message_unlocked(self) -> ChatMessageSnapshot:
        """
        在调用方已持有账号 guard 时读取最后一个可见消息节点。

        无输入；返回稳定快照或确定性空会话；节点过多或消息不确定时抛出 ``ChatSafetyError``。
        """

        snapshots = await self._read_visible_messages_unlocked()
        return snapshots[-1] if snapshots else _empty_conversation_snapshot()

    async def _read_visible_messages_unlocked(self) -> list[ChatMessageSnapshot]:
        """
        在调用方持有账号 guard 时读取全部可见消息快照。

        无输入；按 DOM 顺序返回快照；历史过长、文本为空或方向不明时失败关闭。
        """

        messages = self._page.locator(CHAT_MESSAGE_SELECTOR)
        count = await messages.count()
        if count > MAX_MESSAGE_NODES:
            raise ChatSafetyError("chat_history_too_large", "聊天消息节点数量超出安全上限")
        snapshots: list[ChatMessageSnapshot] = []
        for index in range(count):
            candidate = messages.nth(index)
            if await candidate.is_visible():
                snapshots.append(await _snapshot_message(candidate))
        message_list = await _unique_visible_locator(
            self._page,
            CHAT_MESSAGE_LIST_SELECTOR,
            "消息列表",
        )
        flex_direction = await message_list.evaluate(
            "(node) => window.getComputedStyle(node).flexDirection"
        )
        if flex_direction == "column-reverse":
            # 真实闲鱼页把最新消息放在 DOM 前部；对外统一返回从旧到新的时间顺序。
            snapshots.reverse()
        elif flex_direction != "column":
            raise ChatSafetyError("message_order_not_confirmed", "无法确认聊天消息排列顺序")
        return snapshots

    def _assert_unchanged(
        self, latest: ChatMessageSnapshot, expected_latest_fingerprint: str
    ) -> None:
        """
        比较当前最新消息与调用方读取阶段的指纹。

        参数为当前快照和预期指纹；不一致时抛出 ``ChatSafetyError``；没有页面副作用。
        """

        if latest.fingerprint != expected_latest_fingerprint:
            raise ChatSafetyError(
                "conversation_changed_before_send",
                "最新消息在发送前发生变化，必须重新生成并审核草稿",
            )

    async def _count_matching_own_messages(self, text: str) -> int:
        """
        统计当前可见的本人同文消息，用于排除历史重复文本的误确认。

        参数为草稿文本；返回匹配数量；节点异常向上抛出且只读取 DOM。
        """

        expected = normalize_chat_text(text)
        messages = self._page.locator(OWN_CHAT_MESSAGE_SELECTOR)
        count = 0
        for index in range(await messages.count()):
            node = messages.nth(index)
            if (
                await node.is_visible()
                and normalize_chat_text(await node.inner_text(timeout=2_000)) == expected
            ):
                count += 1
        return count

    async def _wait_for_own_confirmation(
        self, text: str, previous_matching_count: int
    ) -> ChatMessageSnapshot:
        """
        等待可见本人同文消息数量比点击前增加，并返回新增消息快照。

        参数为草稿和点击前数量；固定短轮询内未确认时抛出 ``ChatSafetyError``；只读取页面，
        不会再次点击发送。
        """

        expected = normalize_chat_text(text)
        for _ in range(SEND_CONFIRMATION_ATTEMPTS):
            await self._assert_bound_identity()
            await self._assert_chat_ready()
            messages = self._page.locator(OWN_CHAT_MESSAGE_SELECTOR)
            matching: list[Locator] = []
            for index in range(await messages.count()):
                node = messages.nth(index)
                if (
                    await node.is_visible()
                    and normalize_chat_text(await node.inner_text(timeout=2_000)) == expected
                ):
                    matching.append(node)
            if len(matching) > previous_matching_count:
                message_list = await _unique_visible_locator(
                    self._page,
                    CHAT_MESSAGE_LIST_SELECTOR,
                    "消息列表",
                )
                flex_direction = await message_list.evaluate(
                    "(node) => window.getComputedStyle(node).flexDirection"
                )
                if flex_direction not in {"column", "column-reverse"}:
                    raise ChatSafetyError(
                        "message_order_not_confirmed",
                        "无法确认聊天消息排列顺序",
                    )
                confirmed = matching[0] if flex_direction == "column-reverse" else matching[-1]
                return await _snapshot_message(confirmed, forced_direction="self")
            await self._page.wait_for_timeout(SEND_CONFIRMATION_DELAY_MS)
        raise ChatSafetyError(
            "send_confirmation_missing",
            "点击发送后未能确认本人同文消息，禁止自动重试",
        )
