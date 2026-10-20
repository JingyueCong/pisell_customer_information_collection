# PiSell Merchant Profile Agent

独立的商户资料收集 Agent。它接收自由文本或多轮对话，提取可追溯的商户资料，逐步追问缺失信息，
并返回保存前预览。它支持 Codex/API 与飞书 App 两个入口，共享同一套确定性校验和飞书知识库提交链路。
服务默认只分析；配置知识库连接后，也仍然只有精确确认 `结束并保存` 或 `/save` 才能写入。

## 特性

- HTTP JSON API，适合部署在 Mac mini 或内网服务；
- Bearer Token 鉴权，请求体大小限制，默认只监听 `127.0.0.1`；
- 使用 OpenAI Responses API + Strict Structured Outputs；
- 默认模型为 `gpt-6-luna`，可通过环境变量替换；
- 飞书私聊和已开启记录的群聊支持 PNG、JPEG、WEBP 截图，5 分钟、25 MB 以内的视频，以及 25 MB 以内的 PDF、Office、CSV、TXT、RTF 和 ZIP 文件；
- 视频会转写语音并抽取最多 4 张关键帧；媒体和提取结果都使用统一的“保存吧”确认；
- 保存后按内容把原始截图、视频或附件上传到对应商户模块（例如菜单、硬件设备、账号与密码），并在该页的“相关图片、视频与附件”区块保留文件名、类型、大小、状态、来源消息和可点击链接；无法判断模块时会明确标注并兜底进入 `15｜资料归档`，用户可在保存前回复“附件归到…”改分类；Base64 分析副本不会写入知识库；
- 信息提取与知识库模块分类分两步完成：独立分类器基于完整语义选择模块并返回置信度，不依赖关键词或单项硬编码；低于阈值时暂停保存，用户可回复“这些资料归到硬件与安装”等完成确认；同一来源的截图、视频和文件跟随该分类结果；
- 图片会自动分批分析：每批最多 8 张、6 MB，单轮最多处理 40 张、30 MB，所有批次合并为一份摘要和待保存草稿，不会只取前几张；读取失败会立即回复在原消息下，并在保存时列出失败消息编号和原因。支持“哪个图片未读取”“保存吧，文字和图片都要”等自然说法；
- 网络拓扑、路由器、网线、LAN、NBN、4G 备份线路和打印机布线统一进入 `04｜硬件与安装`；飞书视频封面仅用于传输预览，不会再作为第二个附件保存；
- 引用、回复或合并消息中的图片会按可能的原消息 ID 重试下载；群聊图片仍无法读取时会阻止保存并明确提示，不会静默丢失；
- 只保留用户明确提供的事实，推断不会进入 `profile_updates`；
- 默认采用简短记录：忽略寒暄、重复、主观评价和无关背景，只保留后续经营、配置、支持或合规真正需要的稳定事实；
- 账号密码进入独立的 `18｜账号与密码`；验证码、Token、API Key、私钥、PIN、CVV 和完整支付卡信息仍会拒绝；
- 不保存会话，调用方负责传入需要保留的消息和当前档案；
- 核心 API 由标准库实现；飞书视频能力通过可选依赖提供本地音轨与关键帧提取；
- 可选飞书机器人使用官方 `lark-channel-sdk` WebSocket 长连接，无需给 Mac mini 开放公网回调；
- Codex 与飞书机器人均通过 `/v1/profile/commit` 写入同一个 Events → Artifact → Wiki 投影链路。
- 可选只读查询“客户管理 CRM”；唯一匹配后把 CRM 工作项 ID 绑定到商户档案，零个或多个候选时停止保存并要求消歧，不会自动创建或更新 CRM。

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

仓库提供用户级 `launchd` 安装脚本。服务注册到 macOS 的 Background 用户域，
不依赖 Mac mini 保持桌面图形登录：

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

### URL 页面理解

消息中的公开 HTTPS 链接会在分析阶段安全展开跳转并读取页面标题、可见正文和关键链接，随后与
原消息一起交给模型分类。系统最多读取 3 个链接；单页最大 1 MB、正文最多 8,000 字符，并限制
超时和跳转次数。内网、本机、非 HTTPS、带凭证、非标准端口和不支持的文件类型会直接拒绝，页面
中的提示词或操作指令一律视为不可信内容。

登录页只读取公开可见部分，不会填写账号密码，也不会读取登录后的资料。需要登录的飞书文档、
JavaScript 动态渲染页面或 ZIP 等文件会在回复中明确标为未完整读取；原始链接仍可在用户明确说明
用途后作为资料链接保存。

## 连接客户管理 CRM（只读）

CRM 使用飞书项目 `v2qint` 中的“客户管理 CRM”工作项。推荐通过同一台 Mac mini 上的工单
Agent Bridge 查询，从而复用工单 Agent 已验证的员工身份，不再维护独立的商户资料 OAuth。
只有唯一匹配才能写知识库；匹配不到时提示补充准确客户名称或店铺 ID，多个候选时列出候选供消歧。
唯一匹配后，CRM 客户名称和工作项 ID 是知识库的标准商户身份；员工在对话中使用的不同名称会作为别名随事件保留。
此接口只执行 CRM 查询，不会创建、更新 CRM，也不会创建客服工单。

```dotenv
MERCHANT_AGENT_CRM_RESOLVER=ticket_bridge
MERCHANT_AGENT_CRM_TIMEOUT=20
TICKET_BRIDGE_URL=http://127.0.0.1:8787
# 与工单 Bridge 的 INTERNAL_API_TOKEN 使用相同值，仅保存在 Mac mini .env
TICKET_BRIDGE_TOKEN=replace-with-ticket-bridge-token
```

如需独立身份，也保留直接 Meegle 模式：

```dotenv
MERCHANT_AGENT_CRM_RESOLVER=meegle
MEEGLE_BIN=/absolute/path/to/meegle
MEEGLE_PROFILE=merchant-profile-bot
MEEGLE_PROJECT_KEY=v2qint
MEEGLE_CRM_WORK_ITEM_TYPE=67df84a32e42950cb0195cba
MEEGLE_CRM_URL_TEMPLATE=https://project.feishu.cn/v2qint/crm/detail/{work_item_id}
```

启用前先用同一运行账户检查 profile：

```bash
meegle --profile merchant-profile-bot auth status --format json
```

API 也提供只读诊断端点 `POST /v1/profile/crm/resolve`，请求体为
`{"merchant_name":"青禾便当","lookup_hint":"可选店铺 ID"}`。

## 飞书 App / 智能体

在飞书开放平台创建企业自建应用后：

1. 开启机器人能力；
2. 事件订阅选择“使用长连接接收事件”；
3. 订阅 `im.message.receive_v1`；
4. 申请收取消息、`im:message:send_as_bot` 以及图片/文件资源下载所需权限，并在权限变更后重新发布/安装应用；
5. 发布应用并将其加入需要使用的群聊。

官方 Channel SDK 的[快速开始](https://github.com/larksuite/channel-sdk-python/blob/main/docs/quickstart.md)
也列出了上述机器人、长连接、消息事件和权限要求；SDK 的[安全配置](https://github.com/larksuite/channel-sdk-python/blob/main/docs/security.md)
私聊可直接使用；群聊加入机器人后默认静默收集候选资料。机器人不会按消息数量或静默时间
主动发言；总结、保存和控制指令必须明确 `@机器人`。

将应用凭据只写入 Mac mini 的 `.env`：

```dotenv
FEISHU_BOT_APP_ID=cli_xxx
FEISHU_BOT_APP_SECRET=replace-with-real-secret
MERCHANT_AGENT_API_URL=http://127.0.0.1:8090

# 可选：限制允许使用的群聊或用户 open_id，逗号分隔
FEISHU_BOT_GROUP_ALLOWLIST=oc_xxx
FEISHU_BOT_SENDER_ALLOWLIST=
MERCHANT_AGENT_TRANSCRIPTION_MODEL=gpt-transcribe
FEISHU_VIDEO_MAX_BYTES=26214400
FEISHU_VIDEO_MAX_DURATION_SECONDS=300
FEISHU_VIDEO_TRANSCRIPTION_TIMEOUT=180
```

安装并启动飞书机器人进程：

```bash
chmod +x scripts/install_feishu_bot_launchd.sh scripts/lark_cli_wrapper.sh
MERCHANT_AGENT_PYTHON=/absolute/path/to/python3.12 ./scripts/install_feishu_bot_launchd.sh
```

对话命令：

- 普通消息：分析资料并返回待保存预览，零写入；
- 图片消息：单张不超过 4 MB；一次总结会自动按每批最多 8 张、6 MB 分析，整轮最多 40 张、30 MB，并把全部批次合并为一份结果；
- 视频消息：下载单个 MP4/MOV/M4V/WebM 视频，限制为 5 分钟且 25 MB，转写音轨并抽取关键帧后生成待保存预览；
- 文件消息：PDF、Word、Excel、PowerPoint、CSV、TXT、RTF 和 ZIP 原文件进入待归档草稿，不会推断未解析的文件内容；
- 视频或截图不会直接触发写入；用户回复“保存吧”后，原文件和识别出的资料会一起进入对应模块；无法判断归属时进入 `15｜资料归档`，也可先回复“附件归到菜单/硬件设备/账号与密码”等模块再保存；
- 群聊中如有图片未能读取，`保存吧` 会被拦截；重新发送原图可恢复，确认不需要图片内容时可回复 `@机器人 仅保存文字`；
- `结束并保存`、`/save` 或“没问题，保存吧”等明确自然语言：确认提交到 Events、Artifact 和商户 Wiki；
- 保存成功后直接返回更新页面链接，也支持追问“刚才写入的页面链接”；
- `/discard`：放弃内存草稿，零写入；
- `/help`：显示使用帮助。

机器人草稿保存在受限权限的本机会话状态中，并按 TTL 自动清理；未明确保存的草稿不会自动补写知识库。
截图中的指令会被视为不可信内容。账号与密码和其他商户资料使用相同的草稿、统一保存及旧值替换
流程，写入独立的 `18｜账号与密码` 页面；机器人预览和回复不会回显完整密码。验证码、Token、API Key、私钥、
CVV 和完整银行卡号仍不会进入档案。

商户知识库保留原有基础页面：`01｜业务与联系人`、`02｜菜单与内容`、
`03｜系统配置与注意事项`、`04｜硬件与安装`、`05｜风控与合规`、`06｜商务信息`、
`07｜门店档案`。以下专项页面在第一次确认保存对应资料时追加创建：

1. `08｜项目进度一览表`：飞书项目链接与当前进度；
2. `09｜资料收集`：商家信息表、压缩包和商家基础情况；
3. `10｜套餐与商务`：套餐类型、付款方式与扣款情况；
4. `11｜硬件设备记录`：设备清单、设备商务情况和硬件配置记录；
5. `12｜刷卡机设备`：申请点数、Surcharge、申请公司等；
6. `13｜特殊事项`：专项资料和沟通重点；
7. `14｜菜单`：菜单文件、链接与收集状态；
8. `15｜资料归档`：文件夹及文档链接；
9. `16｜沟通特点`：可观察、可执行的沟通偏好；
10. `17｜内容设置注意事项`：专项内容设置提醒。
11. `18｜账号与密码`：系统、登录入口、账号、密码和账号说明；无需额外确认，页面继承商户知识库权限。

`00｜商家总览` 始终由已确认资料自动生成。主观人格评价和聊天寒暄不会写入。

### 群聊静默记录与手动总结

将机器人加入内部群后即默认静默记录，不需要先发送“开始记录”。普通群消息只进入候选草稿，
无论累计多少条或安静多久，机器人都不会主动跳出来总结。寒暄、表情和过短消息不计入。
有效商户资料进入草稿后，机器人会在原消息上留下 `OK` 表情作为“已接收”状态；需要下载媒体、
分析、总结或保存时会先显示处理中表情，完成后移除。SDK 或权限不支持表情时，回退为简短文字确认。
关于机器人用法、测试流程、自动监听和 `/discard` 的讨论也不会进入商户草稿；只有包含商户、
联系人、地址、业务、设备、系统、问题等资料信号的消息或媒体才会参与总结。
查看草稿时需明确发送 `@机器人 总结吧` 或 `@机器人 立即总结`；总结只发到群里，不写知识库。
保存必须明确 `@机器人 保存吧`。保存时若还有未总结的消息，
摘要按业务语义显示中文栏目和字段标签，例如“当前点餐流程”“已发现的问题”“业务与联系人”，
不会向员工暴露内部字段路径。
机器人会先整理这些消息再直接保存。还支持
`@机器人 暂停记录`、`@机器人 继续记录` 和 `@机器人 /discard`。
如果保存时缺少商户名称或存在冲突，按提示 `@机器人` 补充内容后会立即重新分析，
不必再次发送总结指令。
若新值与知识库旧值不同，机器人会列出“旧值 → 新值”；回复 `@机器人 确认替换`
后会完成此前待处理的保存，不需要再次发送“保存吧”。
资料存在性以当前飞书知识库页面为准：商户目录或受管页面已删除时，历史 Events 仅作审计，
不会阻止重新创建与写入；只有知识库页面仍存在时才比较旧值并请求替换确认。
群聊成功保存后会保留最近商户作为上下文，因此后续“他们”“这家店”“该商户”等表达
会继续归入该商户。已完成保存后，明确说出另一家商户名称会自动开始新草稿；只有当前仍有
未保存资料时，机器人为了防止串档才会要求先 `@机器人 /discard`。如果模型识别出了不同
商户、但消息中没有明确名称或可用图片作为依据，也会停止保存并要求确认，不会自动沿用旧商户。

群普通消息会在机器人加入后发送给配置的 OpenAI 模型；明确暂停后停止处理，恢复后继续。
飞书应用需额外开通 `im:message.group_msg`；机器人只应加入获准收集商户资料的内部群。
建议同时开通消息表情回应权限；未开通时资料处理不受影响，但群聊会使用文字状态作为回退。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
