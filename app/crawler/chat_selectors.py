"""
本文件集中保存闲鱼单商品聊天适配层使用的保守选择器和身份属性名。

它属于 crawler 页面适配模块，只描述允许读取或点击的 DOM 边界，供 ``chat_client``
统一调用。选择器刻意不包含购买、付款、收货地址或确认订单控件；页面结构无法唯一匹配时
由客户端失败关闭。本文件不执行页面操作、不读取登录态，也不包含业务或大模型逻辑。
"""

BODY_SELECTOR = "body"

PRODUCT_IDENTITY_SELECTOR = ", ".join(
    (
        "[data-testid='item-detail'][data-item-id]",
        "[data-role='item-detail'][data-item-id]",
        "[data-xianyu-item-id]",
    )
)
PRODUCT_IDENTITY_ATTRIBUTES = ("data-item-id", "data-xianyu-item-id")

SELLER_IDENTITY_SELECTOR = ", ".join(
    (
        "[data-testid='seller-card'][data-seller-id]",
        "[data-role='seller-card'][data-seller-id]",
        "[data-xianyu-seller-id]",
    )
)
SELLER_IDENTITY_ATTRIBUTES = ("data-seller-id", "data-xianyu-seller-id")

ACCOUNT_IDENTITY_SELECTOR = ", ".join(
    (
        "[data-testid='account-profile'][data-account-id]",
        "[data-role='current-account'][data-account-id]",
        "[data-current-account-id]",
    )
)
ACCOUNT_IDENTITY_ATTRIBUTES = ("data-account-id", "data-current-account-id")

OPEN_CHAT_SELECTOR = ", ".join(
    (
        "button[data-testid='open-chat']",
        "button[data-role='open-chat']",
        "[data-testid='item-contact-seller']",
    )
)
CHAT_PANEL_SELECTOR = ", ".join(
    (
        "[data-testid='chat-panel']",
        "[data-role='chat-panel']",
    )
)
CHAT_INPUT_SELECTOR = ", ".join(
    (
        "textarea[data-testid='chat-input']",
        "textarea[data-role='chat-input']",
        "[contenteditable='true'][data-testid='chat-input']",
    )
)
CHAT_SEND_SELECTOR = ", ".join(
    (
        "button[data-testid='chat-send']",
        "button[data-role='chat-send']",
    )
)
CHAT_MESSAGE_SELECTOR = ", ".join(
    (
        "[data-testid='chat-message']",
        "[data-role='chat-message']",
    )
)
OWN_CHAT_MESSAGE_SELECTOR = ", ".join(
    (
        "[data-testid='chat-message'][data-direction='self']",
        "[data-role='chat-message'][data-direction='self']",
        "[data-testid='chat-message'][data-direction='outgoing']",
        "[data-role='chat-message'][data-direction='outgoing']",
    )
)

MESSAGE_ID_ATTRIBUTES = ("data-message-id", "data-id")
MESSAGE_DIRECTION_ATTRIBUTES = ("data-direction", "data-message-direction", "data-role-type")
MESSAGE_TIMESTAMP_ATTRIBUTES = ("data-timestamp", "data-created-at", "datetime")
