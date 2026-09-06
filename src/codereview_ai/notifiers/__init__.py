"""IM 通知推送（DESIGN F4）：中性通知模型 + 渠道 sink + 分发。"""

from codereview_ai.notifiers.base import (
    Notifier,
    ReviewNotification,
    build_review_notification,
    truncate_utf8,
)
from codereview_ai.notifiers.dingtalk import DingTalkNotifier
from codereview_ai.notifiers.dispatch import NotifierDispatcher, build_notifier
from codereview_ai.notifiers.feishu import FeishuNotifier
from codereview_ai.notifiers.wecom import WeComNotifier

__all__ = [
    "Notifier",
    "ReviewNotification",
    "build_review_notification",
    "truncate_utf8",
    "DingTalkNotifier",
    "FeishuNotifier",
    "WeComNotifier",
    "NotifierDispatcher",
    "build_notifier",
]
