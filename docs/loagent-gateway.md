# LoAgent 网关（`loagent-gateway` 分支）

在 wechat-selkies 原有的网页上加了一个 OneBot v11 接口 `wechat-api`，让 AstrBot 用自带的 `aiocqhttp` 适配器接入微信个人号。

## 镜像里有什么

- 只装微信（不装 QQ 和文件管理器）。微信安装包锁定为 **4.1.13.26**，按 sha256 校验；腾讯的下载地址不带版本号，校验不一致时构建直接失败，要用本地保存的同版本安装包（放进 `build/vendor/`，或者用构建上下文 `wechat_vendor` 指过去）。微信程序目录只读，不会自己升级；不再跟随上游的自动更新。
- `wx`：wx-cli-again，从提交 `077a54cb…` 的源码编译（锁提交和校验和）。
- `wechat-api`：`/opt/wechat-api`，由 s6 服务 `svc-wechat-api` 在桌面启动后运行，监听 `127.0.0.1:8095`；nginx 把 `/api/` 转给它，这一段不受 Selkies 密码限制，用自己的 `access_token`。

## 首次使用

1. 打开 `https://<主机>:3001`，在网页桌面里扫码登录微信。
2. wx-cli 由用户自己在容器里初始化：按 wx-cli-again 自己的文档完成初始化。网关不做这一步，也不会自动调用它。网关以 `abc` 用户、`HOME=/config` 读取，所以 wx-cli 的配置要在 `/config/.wx-cli/`（持久卷）里。初始化之前，`/api/health` 显示「需要用户初始化」，网关不收消息。
3. 初始化完成后，网关在 30 秒内自动发现并开始收消息。

## 接口

鉴权：`Authorization: Bearer <WECHAT_API_TOKEN>`（也接受 `Token <token>`）或 `?access_token=`。没配 `WECHAT_API_TOKEN` 时 OneBot 接口全部关闭。

| 入口 | 说明 |
|---|---|
| `GET /api/health` | 不需要 token。微信进程、登录状态、wx-cli 读取状态（`needs_user_init`）、最后收到消息的时间、发送计数、反向连接状态 |
| `POST /api/onebot/<action>` | `send_group_msg`、`send_private_msg`、`send_msg`、`get_group_list`、`get_group_info`、`get_group_member_list`、`get_group_member_info`、`get_msg`、`get_login_info`、`get_status`、`get_version_info`、`can_send_image`、`can_send_record` |
| `GET /api/onebot/ws` | 正向 WebSocket：推事件、收动作 |
| 反向 WebSocket | 主动连 `ONEBOT_REVERSE_URL`（默认 `ws://astrbot:6199/ws`），带 `X-Self-ID`、`X-Client-Role: Universal`、`Authorization`，断线指数退避重连（最长 60 秒） |

消息格式：

- 群号、用户号是微信原始 id 的稳定哈希（53 位以内），动作参数里既可以给数字，也可以直接给原始 id（`xxx@chatroom`、`wxid_xxx`）。原始 id 放在事件的 `wechat` 字段和 `at` 段的 `wechat_id` 里。
- 收：`text`、`at`（正文里的 `@群昵称` 对照群成员表识别）、`reply`（引用，带 `wechat_quote` 原文）、`image`（尽量用 `wx extract` 导出到 `/config/wechat-api/images/`，导出不了时 `file` 是 `wechat-local://<会话>/<local_id>`）。
- 发：`text`、`at`（原生 @）、`reply`（引用）。暂不支持发图。发送是串行的，发完从读回的消息里核对正文和引用，核对不上记失败，不自动重发。

## 配置

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `WECHAT_API_TOKEN` | 空 | OneBot `access_token` |
| `ONEBOT_REVERSE_URL` | `ws://astrbot:6199/ws` | 反向 WebSocket 地址；留空就不连 |
| `ONEBOT_REVERSE_TOKEN` | 同 `WECHAT_API_TOKEN` | 反向连接带的 token |
| `WECHAT_API_POLL_INTERVAL` | `1.5` | 收消息轮询间隔（秒） |
| `WECHAT_API_MAX_BACKLOG_SECONDS` | `300` | 比这更早的消息只存档不推送（重启后不回复旧消息） |
| `WECHAT_SELF_WXID` / `WECHAT_SELF_NICKNAME` | 空 | 自己的 wxid 和昵称；不填时尽量自动推断，发出第一条群消息并核对成功后会记住 wxid |
| `WECHAT_API_SEND_WAIT_VERIFY` | `false` | 发送动作是否等读回核对完再返回 |
| `WECHAT_API_IMAGE_EXTRACT` | `true` | 收到图片时尝试导出 |
| `WECHAT_API_DEBUG_SCREENSHOTS` | `false` | 发送的每一步截图到 `/config/wechat-api/screenshots/` |

界面操作的快捷键、等待时间、引用的点击位置都在 `/config/wechat-api/ui.json` 里覆盖，默认值见 `wechat-api/src/wechat_api/config.py` 的 `DEFAULT_UI`。**界面引用默认关闭**（`quote.enabled=false`，降级成文字引用）：右键菜单选错项可能误操作，要在真实客户端上校准 `quote.other_last_point` / `self_last_point` 和 `menu_keys`（或 `menu_offset`）之后再打开。目前界面引用只支持「被引用的是会话里最新的一条」，其余情况降级成文字。

## 开发

```bash
cd wechat-api
uv sync
uv run pytest -q
```
