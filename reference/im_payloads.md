# IM 推送：消息体格式与签名算法

> 来源：`AI-Codereview-Gitlab` 的 `biz/utils/im/{dingtalk,feishu,wecom}.py`。
> 这部分的**协议细节**（签名算法、消息体结构、字段限制）是可以直接复用的，
> 但原实现有几个问题必须改（见文末）。

---

## 1. 钉钉（DingTalk）

### 加签算法（可直接复用）

```python
import base64, hashlib, hmac, time, urllib.parse

def sign_dingtalk_url(webhook_url: str, secret: str) -> str:
    """钉钉自定义机器人「加签」安全设置的 URL 构造。

    注意：timestamp 与服务端时差不能超过 1 小时，
    且**每次发送都要重新计算**——原项目在 __init__ 里算一次就固定了，
    进程跑超过 1 小时后所有推送都会失败（见文末问题 1）。
    """
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"{webhook_url}&timestamp={timestamp}&sign={sign}"
```

### 消息体

```jsonc
// markdown 类型
{
  "msgtype": "markdown",
  "markdown": {
    "title": "代码审查报告",        // 必填，出现在会话列表的摘要里
    "text": "#### 项目 xxx\n> 总分 85\n\n[查看详情](https://...)"
  },
  "at": {
    "atMobiles": ["13800138000"],  // 按手机号 @，需在 text 里同时出现 @13800138000
    "atUserIds": ["user123"],      // 按钉钉 userId @
    "isAtAll": false
  }
}
```

**坑**：钉钉 markdown 里 `@某人` 必须**同时**满足两个条件——`at.atMobiles` 里有这个号码，
且 `text` 正文里出现 `@13800138000` 字样。只做其中一个不会生效。

**限制**：markdown text 上限约 20000 字节；机器人每分钟最多 20 条，超了会被限流 10 分钟。

---

## 2. 飞书（Feishu / Lark）

### 加签算法

```python
def sign_feishu(secret: str) -> tuple[str, str]:
    """飞书的签名与钉钉不同：
    - string_to_sign 是 f"{timestamp}\n{secret}"，但用它作为 **key**，对空字节串做 HMAC
    - timestamp 是**秒**不是毫秒
    - 签名放在 body 里，不是 URL 参数
    """
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256).digest()
    return timestamp, base64.b64encode(hmac_code).decode("utf-8")
```

### 消息体（推荐用交互式卡片，比 text 好看很多）

```jsonc
{
  "timestamp": "1700000000",   // 开启签名校验时必填
  "sign": "xxxx",
  "msg_type": "interactive",
  "card": {
    "config": {"wide_screen_mode": true},
    "header": {
      "title": {"tag": "plain_text", "content": "代码审查报告"},
      "template": "red"        // red/orange/green/blue —— 可按评分动态着色
    },
    "elements": [
      {"tag": "div", "text": {"tag": "lark_md", "content": "**项目**：xxx\n**总分**：85"}},
      {"tag": "hr"},
      {"tag": "div", "text": {"tag": "lark_md", "content": "审查详情..."}},
      {"tag": "action", "actions": [
        {"tag": "button", "text": {"tag": "plain_text", "content": "查看 MR"},
         "url": "https://...", "type": "primary"}
      ]}
    ]
  }
}
```

按评分动态设置 `header.template` 颜色（<60 红 / <80 橙 / ≥80 绿）是个很小但很讨喜的细节。

**注意**：飞书用的是 `lark_md` 而不是标准 markdown，**不支持表格**，
标题只支持 `**加粗**`。原项目直接把 LLM 输出的标准 markdown 塞进去，表格会显示成乱码。
新项目需要一个 markdown → lark_md 的转换层。

---

## 3. 企业微信（WeCom）

```jsonc
{
  "msgtype": "markdown",
  "markdown": {
    "content": "# 代码审查\n> 总分 <font color=\"warning\">85</font>"
  }
}
```

企业微信机器人**不支持签名**，安全性靠 webhook key 本身的保密性。

**限制**：
- content 上限 **4096 字节**（三家里最小，很容易超）
- 不支持 `@` 特定成员（只能 `<@userid>` 语法，且需要在 content 里）
- 支持有限的 HTML 着色：`<font color="info|comment|warning">`

**超长处理**：LLM 的 review 结果经常超过 4096 字节。
原项目的做法是直接发，超了就失败。正确做法是：截断 + 附一条"查看完整报告"的链接。

---

## 4. 统一抽象建议

三家的差异集中在：签名方式、消息体结构、长度限制、markdown 方言。
建议这样抽象：

```python
class Notifier(Protocol):
    name: str
    max_content_bytes: int

    async def send(self, msg: ReviewNotification) -> None: ...

@dataclass
class ReviewNotification:
    """中立的通知模型，由各 Notifier 自行渲染成平台格式。"""
    project_name: str
    title: str
    score: int | None
    summary_md: str          # 标准 markdown
    url: str                 # MR/PR 链接
    findings_count: dict[str, int]   # {"blocker": 1, "major": 3, ...}
    at_users: list[str]      # 平台无关的用户标识，由 Notifier 映射
```

每个 Notifier 负责：标准 markdown → 平台方言、超长截断、签名、@ 映射。

---

## 5. 原实现必须改的问题

1. **钉钉签名只在 `__init__` 算一次**（`dingtalk.py:17-27`）。
   timestamp 固定，进程运行超过 1 小时后**所有推送全部失败**。签名必须在每次 send 时计算。

2. **加签失败时 `default_webhook_url` 未赋值**（`dingtalk.py:17-31` 的 try 分支）。
   异常被 catch 后 else 分支不执行，属性根本不存在，后续访问抛 AttributeError。

3. **把含 token 的签名 URL 打进日志**（`dingtalk.py:27`）。

4. **零 timeout**（`dingtalk.py:99` / `feishu.py:111` / `wecom.py:163`）。

5. **`logger.error(f"...", e)` 吞掉异常**（`dingtalk.py:106`）——
   f-string 已经格式化完了，多传的 `e` 会被 logging 忽略，堆栈完全丢失。
   应该用 `logger.error("...: %s", e, exc_info=True)`。

6. **按项目路由 webhook 靠遍历环境变量**（`dingtalk.py:49-58`）——
   `DINGTALK_WEBHOOK_URL_{PROJECT_NAME}` 这种约定在项目名含特殊字符时会失效，
   且无法在运行时修改。新项目改为数据库里的项目级配置。

7. **无重试**。IM 网关偶发 5xx 很常见，应该有 2-3 次指数退避重试。
   但要注意**幂等**——重试可能导致重复推送，需要在应用层去重。
