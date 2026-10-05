# PiSell Merchant Profile Agent

独立的商户资料收集 Agent。它接收自由文本或多轮对话，提取可追溯的商户资料，逐步追问缺失信息，
并返回保存前预览。服务默认只分析、不写入飞书或其他业务系统。

## 特性

- HTTP JSON API，适合部署在 Mac mini 或内网服务；
- Bearer Token 鉴权，请求体大小限制，默认只监听 `127.0.0.1`；
- 使用 OpenAI Responses API + Strict Structured Outputs；
- 默认模型为 `gpt-6-luna`，可通过环境变量替换；
- 只保留用户明确提供的事实，推断不会进入 `profile_updates`；
- 自动拒绝密码、验证码、Token、API Key、CVV 等敏感字段；
- 不保存会话，调用方负责传入需要保留的消息和当前档案；
- 标准库实现，无运行时第三方 Python 依赖。

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
`next_question` 和 `save_readiness`。`write_performed` 永远是 `false`，调用方必须在用户确认后自行决定
是否写入业务系统。

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

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
