# PiSell Merchant Profile Agent

独立的商户资料收集 Agent。它接收自由文本或多轮对话，提取可追溯的商户资料，逐步追问缺失信息，
并返回保存前预览。它支持 Codex/API 与飞书 App 两个入口，共享同一套确定性校验和飞书知识库提交链路。
服务默认只分析；配置知识库连接后，也仍然只有精确确认 `结束并保存` 或 `/save` 才能写入。

## 特性

- HTTP JSON API，适合部署在 Mac mini 或内网服务；
- Bearer Token 鉴权，请求体大小限制，默认只监听 `127.0.0.1`；
- 使用 OpenAI Responses API + Strict Structured Outputs；
- 默认模型为 `gpt-6-luna`，可通过环境变量替换；
- 只保留用户明确提供的事实，推断不会进入 `profile_updates`；
- 默认采用简短记录：忽略寒暄、重复、主观评价和无关背景，只保留后续经营、配置、支持或合规真正需要的稳定事实；
- 自动拒绝密码、验证码、Token、API Key、CVV 等敏感字段；
- 不保存会话，调用方负责传入需要保留的消息和当前档案；
- 标准库实现，无运行时第三方 Python 依赖。
- 可选飞书机器人使用官方 `lark-channel-sdk` WebSocket 长连接，无需给 Mac mini 开放公网回调；
- Codex 与飞书机器人均通过 `/v1/profile/commit` 写入同一个 Events → Artifact → Wiki 投影链路。

OpenAI API 的结构化输出格式参考[官方 Structured Outputs 文档](https://developers.openai.com/api/docs/guides/structured-outputs)，默认模型能力参考[GPT-6 Luna 文档](https://developers.openai.com/api/docs/models/gpt-6-luna)。

## 本地运行

```bash
cp .env.example .env
# 编辑 .env，设置 MERCHANT_AGENT_API_KEY 和 OPENAI_API_KEY
python3 -m venv .venv
PYTHONPATH=src .venv/bin/python -m merchant_profile_agent --env-file .env
```

健康检查：

```bash
curl http://127.0.0.1:8090/healthz
```

API 文档：

```bash
curl http://127.0.0.1:8090/openapi.json
```

## 分析一段资料

```bash
curl -sS http://127.0.0.1:8090/v1/profile/analyze \
  -H "Authorization: Bearer $MERCHANT_AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {
        "role": "user",
        "content": "青禾便当在墨尔本有两家门店，主要联系人是林经理。",
        "source_ref": "crm://note/123"
      }
    ],
    "current_profile": {}
  }'
```

响应包含 `profile_updates`、`assumptions`、`information_gaps`、`conflicts`、
`next_question` 和 `save_readiness`。分析接口的 `write_performed` 永远是 `false`；只有提交接口能够写入。

## 仅做确定性校验

`POST /v1/profile/validate` 不调用模型，可用于保存前再次校验：

```json
{
  "merchant_name": "青禾便当",
  "updates": [
    {
      "field_path": "contacts.primary.name",
      "value": "林经理",
      "source_ref": "crm://note/123",
      "replace_confirmed": false
    }
  ],
  "current_profile": {}
}
```

## Mac mini 部署

仓库提供用户级 `launchd` 安装脚本：

```bash
cp .env.example .env
chmod 600 .env
./scripts/install_launchd.sh
curl http://127.0.0.1:8090/healthz
```

如果系统 `python3` 低于 3.11，可显式指定已有的 Python：

```bash
MERCHANT_AGENT_PYTHON=/absolute/path/to/python3.12 ./scripts/install_launchd.sh
```

服务日志位于：

```text
~/Library/Logs/Pisell/merchant-profile-agent.log
~/Library/Logs/Pisell/merchant-profile-agent.error.log
```

若需从其他机器访问，建议继续监听 `127.0.0.1`，通过 Tailscale、SSH tunnel 或带 TLS 的反向代理
暴露服务；不要直接把未加密端口开放到公网。

## 连接飞书知识库

当前生产数据层由 P1 的不可变 Events 和托管 Wiki 投影负责。独立 API 通过适配器复用这条链路，
不会让模型直接操作飞书。Mac mini 的 `.env` 增加：

```dotenv
MERCHANT_AGENT_KNOWLEDGE_STORE=p1
PISELL_P1_ROOT=/Users/piai/Pisell/P1
PISELL_FEISHU_DATA_LAYER_CONFIG=/Users/piai/Pisell/P1/config/feishu-data-layer.json
LARK_CLI=/Users/piai/Services/pisell_customer_information_collection/scripts/lark_cli_wrapper.sh
```

读取当前商户档案：

```http
POST /v1/profile/read
Authorization: Bearer <MERCHANT_AGENT_API_KEY>
Content-Type: application/json

{"merchant_name":"青禾便当","merchant_id":null}
```

提交时必须带精确确认词、完整消息来源以及已经通过预览的更新：

```json
{
  "confirmation": "结束并保存",
  "conversation_id": "codex-or-feishu-session-id",
  "merchant": {"name": "青禾便当", "id": null},
  "messages": [
    {
      "role": "user",
      "content": "青禾便当的主要联系人改为陈经理，我确认替换旧值。",
      "source_ref": "feishu://chat/oc_xxx/message/om_xxx"
    }
  ],
  "updates": [
    {
      "field_path": "contacts.primary.name",
      "value": "陈经理",
      "source_ref": "feishu://chat/oc_xxx/message/om_xxx",
      "replace_confirmed": true
    }
  ]
}
```

API 会在写入前重新读取飞书中的最新 Event 链并再次检查冲突。相同会话和快照重复提交会沿用 P1 的
幂等键，不会制造重复档案。

## 飞书 App / 智能体

在飞书开放平台创建企业自建应用后：

1. 开启机器人能力；
2. 事件订阅选择“使用长连接接收事件”；
3. 订阅 `im.message.receive_v1`；
4. 申请收取消息以及 `im:message:send_as_bot` 权限；
5. 发布应用并将其加入需要使用的群聊。

官方 Channel SDK 的[快速开始](https://github.com/larksuite/channel-sdk-python/blob/main/docs/quickstart.md)
也列出了上述机器人、长连接、消息事件和权限要求；SDK 的[安全配置](https://github.com/larksuite/channel-sdk-python/blob/main/docs/security.md)
私聊可直接使用；群聊必须先 `@机器人 开启自动总结`，开启后才会接收普通群消息，
保存和控制指令仍必须明确 `@机器人`。

将应用凭据只写入 Mac mini 的 `.env`：

```dotenv
FEISHU_BOT_APP_ID=cli_xxx
FEISHU_BOT_APP_SECRET=replace-with-real-secret
MERCHANT_AGENT_API_URL=http://127.0.0.1:8090

# 可选：限制允许使用的群聊或用户 open_id，逗号分隔
FEISHU_BOT_GROUP_ALLOWLIST=oc_xxx
FEISHU_BOT_SENDER_ALLOWLIST=
```

安装并启动飞书机器人进程：

```bash
chmod +x scripts/install_feishu_bot_launchd.sh scripts/lark_cli_wrapper.sh
MERCHANT_AGENT_PYTHON=/absolute/path/to/python3.12 ./scripts/install_feishu_bot_launchd.sh
```

对话命令：

- 普通消息：分析资料并返回待保存预览，零写入；
- `结束并保存`、`/save` 或“没问题，保存吧”等明确自然语言：确认提交到 Events、Artifact 和商户 Wiki；
- 保存成功后直接返回更新页面链接，也支持追问“刚才写入的页面链接”；
- `/discard`：放弃内存草稿，零写入；
- `/help`：显示使用帮助。

机器人草稿只保存在内存中，并按 TTL 自动清理。服务重启后未保存草稿会消失，不会自动补写。

### 群聊自动总结

将机器人加入内部群后，先表达开启意图，例如 `@机器人 开始记录`、
`@机器人 接下来帮我收集商户资料` 或 `@机器人 从现在开始整理群聊`。
机器人理解常见近义表达，并排除“不要记录”“暂停记录”等相反语义。开启后，累计 20 条有效消息会自动总结；
若已有至少 5 条消息但群聊安静 10 分钟，也会自动总结。寒暄、表情和过短消息不计入。
自动总结只发到群里，不写知识库；保存必须明确 `@机器人 保存吧`。保存时若还有未总结的消息，
机器人会先整理这些消息再直接保存。还支持
`@机器人 立即总结`、`@机器人 暂停自动总结` 和 `@机器人 /discard`。
如果保存时缺少商户名称或存在冲突，按提示 `@机器人` 补充内容后会立即重新分析，
不必等待下一次自动总结。
若新值与知识库旧值不同，机器人会列出“旧值 → 新值”；回复 `@机器人 确认替换`
后会完成此前待处理的保存，不需要再次发送“保存吧”。
群聊成功保存后会保留最近商户作为上下文，因此后续“他们”“这家店”“该商户”等表达
会继续归入该商户；切换到另一家商户前请先 `@机器人 /discard`。

群普通消息会在该群明确开启后发送给配置的 OpenAI 模型。飞书应用需额外开通
`im:message.group_msg`；外部群仍应保持关闭。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
