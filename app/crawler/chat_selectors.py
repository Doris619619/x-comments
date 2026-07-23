"""
本文件集中保存经 2026-07-23 生产登录态只读标定确认的闲鱼聊天选择器。

它属于 crawler 页面适配模块，只描述商品聊天入口、聊天主体、输入、发送和消息节点。
选择器优先使用 URL 参数、元素语义和稳定类名前缀，不保存哈希后缀、账号、卖家、聊天正文
或 Cookie，也不包含购买、付款、地址和确认订单控件。页面结构漂移时客户端必须失败关闭。
"""

BODY_SELECTOR = "body"

# 商品页只有同时携带商品和对端用户参数的 IM 链接，才可能成为受控聊天入口。
OPEN_CHAT_SELECTOR = "a[href*='/im?'][href*='itemId='][href*='peerUserId=']"
CHAT_PANEL_SELECTOR = "main[class*='chat-main--']"
CHAT_MESSAGE_LIST_SELECTOR = "div[class*='message-list-reverse--']"
CHAT_INPUT_SELECTOR = "textarea[placeholder='请输入消息，按Enter键发送或点击发送按钮发送']"
CHAT_SEND_SELECTOR = "div[class*='sendbox-bottom--'] > button"

# 每条消息内容节点包含 left/right 方向类；系统商品卡也会成为基线，但不会被当成回复文本发送。
CHAT_MESSAGE_SELECTOR = ", ".join(
    (
        f"{CHAT_MESSAGE_LIST_SELECTOR} div[class*='message-content--'] > "
        "div[class*='msg-text-left--']",
        f"{CHAT_MESSAGE_LIST_SELECTOR} div[class*='message-content--'] > "
        "div[class*='msg-text-right--']",
    )
)
OWN_CHAT_MESSAGE_SELECTOR = (
    f"{CHAT_MESSAGE_LIST_SELECTOR} div[class*='message-content--'] > div[class*='msg-text-right--']"
)

MESSAGE_ID_ATTRIBUTES = ("data-message-id", "data-id")
MESSAGE_DIRECTION_ATTRIBUTES = ("class",)
MESSAGE_TIMESTAMP_ATTRIBUTES = ("data-timestamp", "data-created-at", "datetime")

# 当前登录买家只通过服务端 Cookie 的指纹绑定；原值不得进入日志、事件或数据库。
ACCOUNT_IDENTITY_COOKIE_NAME = "tracknick"
