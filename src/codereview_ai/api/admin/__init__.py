"""后台管理 REST 子包（DESIGN §14）。"""

from __future__ import annotations

from codereview_ai.api.admin import models, notifiers, projects, reviews, tasks

__all__ = ["models", "notifiers", "projects", "reviews", "tasks"]
