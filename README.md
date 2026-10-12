# EvoAgent PR Reviewer

- 审查统一 diff，输出结构化问题、修复建议和测试建议
- GitHub `pull_request` webhook（`opened`、`reopened`、`synchronize`）
- `agentic` 运行模式：Lead、Security、Correctness/Reliability、Critic 多 Agent 协作
- SQLite 保存任务状态、执行轨迹和最终报告
- JSON API 与 Markdown 报告
- webhook HMAC-SHA256 签名校验，以及可选的 GitHub PR 评论回写
- Web 管理台、任务 Dashboard 与 Prometheus 指标
- 普通任务采用固定单轮多 Agent 协作（Lead 2 次、双 Worker 各 1 次、Critic 1 次，共 5 次 LLM 调用）；高风险任务最多允许一轮 Worker 返工
- LLM unified patch、AST/CST、沙箱前后测试对比与仅 Draft PR 的修复闭环
- PostgreSQL、Redis 生产模式
- 失败案例回流、提示词评测、版本激活与回滚
- 自研 Agent Runtime、持久化 checkpoint、执行预算与任务断点续跑
- 带 Tool Registry、参数 Schema 校验和结构化 Observation 的 Agent Loop
- 确认反馈的租户/仓库隔离记忆、检索与过期清理
- Redis Streams ACK、Worker 租约、指数退避重试和死信队列
- Webhook delivery 幂等、重放时间窗与评论 upsert
- 用户登录、RBAC、租户/仓库隔离和不可变管理审计
- 动态 Skill manifest 校验、签名校验和隔离进程沙箱
- 自动修复后的编译/测试门禁、灰度发布与影子流量
- OpenTelemetry Trace、Prometheus 指标和持久化告警

## 快速开始

项目使用 Python 3.11。先安装锁定范围内的运行依赖，并在同一个 PowerShell 窗口中配置本地管理员：

```powershell
python -m pip install -r requirements.txt

$bytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$env:EVOAGENT_AUTH_REQUIRED = 'true'
$env:EVOAGENT_AUTH_SECRET = [Convert]::ToBase64String($bytes)
$env:EVOAGENT_BOOTSTRAP_ADMIN_USERNAME = 'admin'
$env:EVOAGENT_BOOTSTRAP_ADMIN_PASSWORD = '<替换为至少 10 个字符的密码>'

python -m evoagent
```

不要直接使用示例占位符作为密码或密钥。环境变量只对当前 PowerShell 及其子进程生效；修改配置后需要停止并重新启动 EvoAgent。服务可以在未配置模型时启动并提供健康检查，但提交审查前必须按下方“模型配置”章节配置模型。

Bootstrap 管理员只在用户名尚不存在时创建；已有同名用户的密码不会在重启时被覆盖。

服务默认监听 `127.0.0.1:8080`。启动后打开 `http://127.0.0.1:8080/`，前端会在业务 API 返回未授权状态后显示登录层。登录状态保存在当前浏览器的 `localStorage` 中；需要重新登录时可以点击退出，或清除站点数据。

API 调用需要先登录并携带 Bearer Token：

```powershell
$session = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/v1/auth/login `
  -ContentType 'application/json' `
  -Body (@{username='admin'; password='<你的密码>'} | ConvertTo-Json)
$headers = @{Authorization="Bearer $($session.access_token)"}
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/v1/reviews `
  -Headers $headers `
  -ContentType 'application/json' `
  -Body (@{
    repository = 'demo/api'
    pull_request = 12
    mode = 'agentic'
    diff = "diff --git a/app.py b/app.py`n--- a/app.py`n+++ b/app.py`n@@ -1 +1,2 @@`n+password = 'secret'`n+eval(user_input)"
  } | ConvertTo-Json)
```

查询任务：

```powershell
Invoke-RestMethod -Headers $headers http://127.0.0.1:8080/v1/tasks/<task-id>
Invoke-WebRequest -Headers $headers http://127.0.0.1:8080/v1/tasks/<task-id>/report
```

运行测试：

```powershell
python -m unittest discover -s tests -v
```

## 模型配置

DeepSeek 官方 API（按 Token 计费）：

```powershell
$env:EVOAGENT_LLM_PROVIDER = 'deepseek'
$env:EVOAGENT_DEEPSEEK_API_KEY = '<deepseek-api-key>'
python -m evoagent
```

通过 OpenRouter 使用有速率限制、可用性可能变化的 DeepSeek 免费模型：

```powershell
$env:EVOAGENT_LLM_PROVIDER = 'openrouter-deepseek-free'
$env:EVOAGENT_OPENROUTER_API_KEY = '<openrouter-api-key>'
python -m evoagent
```

如果指定的免费 DeepSeek 版本下线，可将 `EVOAGENT_LLM_MODEL` 改为 OpenRouter 当前提供的其他 `:free` 模型，或把 Provider 改为 `openrouter-free` 让免费路由自动选择可用模型。

任意其他 OpenAI Chat Completions 兼容端点使用 `custom`：

```powershell
$env:EVOAGENT_LLM_PROVIDER = 'custom'
$env:EVOAGENT_LLM_BASE_URL = 'https://example.com/v1'
$env:EVOAGENT_LLM_API_KEY = '<token>'
$env:EVOAGENT_LLM_MODEL = '<model-name>'
```

密钥只通过环境变量读取，不要提交到仓库。

项目启动时会自动读取项目根目录的 `.env`，也兼容 `evoagent/.env`；系统环境变量优先于 `.env` 文件。推荐将以下内容写入根目录 `.env`（该文件已被 `.gitignore` 忽略）：

```env
EVOAGENT_LLM_PROVIDER=deepseek
EVOAGENT_DEEPSEEK_API_KEY=你的真实APIKey
```

## GitHub Webhook

项目使用“GitHub 仓库 Webhook + 公网转发 + fine-grained PAT”接收 PR 事件，不需要创建或安装 GitHub App：

```text
GitHub Pull request 事件
        │
        ▼
https://<公网域名>/webhooks/github
        │  公网转发
        ▼
http://127.0.0.1:8080/webhooks/github
        │
        ▼
EvoAgent 创建异步审查任务
```

### 1. 配置 EvoAgent

先生成一个 Webhook Secret，并根据需要配置 GitHub fine-grained personal access token：

```powershell
$webhookBytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($webhookBytes)
$env:EVOAGENT_GITHUB_WEBHOOK_SECRET = [Convert]::ToBase64String($webhookBytes)

# 私有仓库、PR 评论回写或自动修复需要；只审查公开仓库且不回写时可以不配置。
$env:EVOAGENT_GITHUB_TOKEN = '<GitHub fine-grained PAT>'

# 默认关闭。设为 true 后，审查完成时更新或创建 PR 评论。
$env:EVOAGENT_AUTO_POST_REVIEW = 'true'

python -m evoagent
```

Webhook Secret 用于验证 GitHub 请求头中的 HMAC-SHA256 签名，不能与登录用的 `EVOAGENT_AUTH_SECRET` 混用。Webhook 请求不携带管理台 Bearer Token；`/webhooks/github` 使用签名而不是用户登录进行认证。

fine-grained PAT 只授权需要接入的仓库，并按功能授予最小权限：

- 读取私有仓库 PR Diff：`Contents: Read`、`Pull requests: Read`；
- 回写审查评论：`Pull requests: Read and write`；
- 创建自动修复分支和提交：`Contents: Read and write`、`Pull requests: Read and write`。

只接收 Webhook 但不访问私有仓库、不回写评论且不执行自动修复时，可以不设置 PAT。密钥必须在启动 EvoAgent 前设置，修改后需要重启服务。

### 2. 建立公网转发

GitHub 无法访问 `127.0.0.1`，需要把公网 HTTPS 地址转发到本地 `http://127.0.0.1:8080`。任选一种已安装的转发工具，例如：

```powershell
# Cloudflare Quick Tunnel
cloudflared tunnel --url http://127.0.0.1:8080

# 或 ngrok
ngrok http 8080
```

命令启动后会显示一个形如 `https://example.trycloudflare.com` 或 `https://example.ngrok-free.app` 的公网 HTTPS 地址。保持 EvoAgent 和转发进程同时运行。临时公网地址通常会在转发工具重启后变化，变化后必须同步更新 GitHub Webhook 的 Payload URL。

上述快捷转发会把 8080 端口上的管理台和 API 一并暴露到公网，因此必须保持 `EVOAGENT_AUTH_REQUIRED=true`，并使用强管理员密码和随机 `EVOAGENT_AUTH_SECRET`。长期部署建议通过反向代理只公开 `/webhooks/github`（以及按需公开 `/health`），不要向公网暴露整个管理台。

### 3. 在 GitHub 仓库中添加 Webhook

进入目标仓库的 **Settings → Webhooks → Add webhook**，填写：

- **Payload URL**：`https://<公网域名>/webhooks/github`；
- **Content type**：`application/json`；
- **Secret**：与 `EVOAGENT_GITHUB_WEBHOOK_SECRET` 完全相同；
- **SSL verification**：保持启用；
- **Which events would you like to trigger this webhook?**：选择 **Let me select individual events**，只勾选 **Pull requests**；
- **Active**：保持勾选。

EvoAgent 会处理 `opened`、`reopened` 和 `synchronize` 三种 PR 动作；其他 `pull_request` 动作会正常接收但被忽略。服务会根据 payload 中的 `diff_url` 下载 Diff，并异步创建审查任务。

### 4. 验证连接

先确认本地服务和公网地址都能访问健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health
Invoke-RestMethod https://<公网域名>/health
```

然后新建 PR、重新打开 PR，或向 PR 推送一次提交。在 GitHub 的 **Settings → Webhooks → Recent Deliveries** 中应看到 `/webhooks/github` 返回 `202`；管理台的任务中心随后会出现对应审查任务。如果失败，优先检查公网转发进程是否仍在运行、Payload URL 是否包含 `/webhooks/github`、Secret 是否一致，以及 PAT 是否有目标仓库权限。

默认只在管理台保存结果。只有 `EVOAGENT_AUTO_POST_REVIEW=true` 时才会向 PR 回写评论。

自动修复只覆盖可确定安全的规则，例如调试输出、`shell=True` 和硬编码 Python 凭据；结果始终提交到新的 `evoagent/fix-pr-*` 分支，不直接修改源分支。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查 |
| `POST` | `/v1/auth/login` | 登录并获取租户绑定的短期 Bearer Token |
| `POST` | `/v1/reviews` | 创建同步审查任务 |
| `POST` | `/v1/reviews?async=true` | 创建异步审查任务 |
| `GET` | `/v1/tasks/{id}` | 获取状态、轨迹和报告 |
| `GET` | `/v1/tasks/{id}/report` | 获取 Markdown 报告 |
| `GET` | `/v1/tasks/{id}/feedback` | 获取该已完成任务的反馈历史 |
| `POST` | `/v1/tasks/{id}/fix` | 创建自动修复分支和提交 |
| `POST` | `/v1/tasks/{id}/feedback` | 回流误报、漏报或坏修复 |
| `POST` | `/v1/tasks/{id}/cancel` | 请求取消任务 |
| `POST` | `/v1/tasks/{id}/resume` | 从最近 checkpoint 续跑任务 |
| `POST` | `/webhooks/github` | 接收 GitHub PR webhook |
| `POST` | `/v1/skills/reload` | 动态重新加载 Skill |
| `POST` | `/v1/evolution/auto` | 从失败案例生成并评测提示词版本 |
| `POST` | `/v1/evolution/propose` | 评测指定提示词候选版本 |
| `GET/POST` | `/v1/evaluation/cases` | 查询或增加版本化评测样本 |
| `GET` | `/v1/evolution/status` | 查询模型与评测门禁就绪状态 |
| `GET` | `/v1/evolution/runs` | 查询持久化的新旧版本评测记录 |
| `POST` | `/v1/skills/{name}/versions/{version}/activate` | 激活或回滚版本 |
| `POST` | `/v1/skill-evolution/auto` | 从确认反馈生成、回放并门禁 Skill 候选 |
| `POST` | `/v1/skill-evolution/propose` | 评测指定 Agent Skill `SKILL.md` artifact |
| `GET` | `/v1/skill-evolution/status?skill_name={name}` | 查询 Skill 门禁与激活版本 |
| `GET` | `/v1/skill-evolution/runs` | 查询 Skill 进化运行与指标 |
| `GET` | `/v1/skill-evolution/{name}/versions` | 查询 Skill artifact 版本链 |
| `POST` | `/v1/skill-evolution/{name}/versions/{version}/activate` | 激活或回滚 Skill artifact |
| `GET` | `/metrics` | Prometheus 文本指标 |
| `GET` | `/api/alerts` | 查询租户告警 |
| `GET` | `/api/audit` | 查询租户审计日志 |
| `GET` | `/api/queue/dead-letters` | 查询死信任务 |
| `POST` | `/v1/queue/dead-letters/replay` | 重放死信任务 |
| `GET/POST` | `/api/deployments/llm-review`、`/v1/deployments/llm-review` | 查询或配置灰度/影子发布 |

`POST /v1/reviews` 的 `diff` 最大默认 1 MiB；单任务默认最多 8 步、120 秒。可通过环境变量调整，详见 `.env.example`。

## Prompt 进化

Prompt 进化需要显式调用 `POST /v1/evolution/auto`（下文简称 `/auto`）或现有的候选评测接口；收集失败反馈不会自动启动后台进化循环。`/auto` 使用失败样本生成候选，`POST /v1/evolution/propose` 仍接受原有的 `skill_name` 和纯文本 `prompt` 请求，并用空的新结构化策略评测该候选。

结构化候选保留全部旧的顶层字段，并可增加 `role_policies`：

```json
{
  "prompt_additions": ["只报告新增行引入且有直接证据的问题。"],
  "few_shot_examples": [],
  "lead_delegation_rules": [],
  "tool_selection_policy": [],
  "budget_parameters": {"security": 4000},
  "role_policies": {
    "lead": {"prompt_additions": [], "few_shot_examples": [], "delegation_rules": []},
    "security": {"prompt_additions": [], "few_shot_examples": [], "tool_selection_policy": []},
    "correctness-reliability": {"prompt_additions": [], "few_shot_examples": [], "tool_selection_policy": []},
    "critic": {"prompt_additions": [], "few_shot_examples": [], "tool_selection_policy": []}
  }
}
```

上述 JSON 是生成器候选及持久化策略的结构；现有 `/propose` 的纯文本请求格式没有改为结构化请求。角色键只允许精确的 `lead`、`security`、`correctness-reliability` 和 `critic`，未提供 `role_policies` 时按空角色策略加载。

| 字段 | 分发或执行方式 |
|---|---|
| 顶层 `prompt_additions`、`few_shot_examples` | 所有角色共享；全局 prompt 已包含的共享增量不重复渲染 |
| 顶层 `lead_delegation_rules` | 仅 Lead 接收，保留旧候选兼容性 |
| 顶层 `tool_selection_policy` | 保留旧的所有角色共享行为；新候选优先放到对应角色下 |
| 顶层 `budget_parameters` | 按角色控制运行时 token 总预算，绝不渲染为提示词文字 |
| `role_policies.<role>.prompt_additions`、`few_shot_examples` | 只进入该角色的系统提示词 |
| `role_policies.lead.delegation_rules` | 只进入 Lead 的分工策略 |
| 其他三个角色的 `tool_selection_policy` | 只进入对应角色的工具指导；工具权限仍由运行时检查 |

各角色仍使用原有的内置提示词，随后追加生效的全局 prompt、共享旧字段和该角色策略。Lead 选中的 Agent Skill 完整指令只追加给对应的 Security 或 Correctness/Reliability Worker；Lead 和 Critic 不接收 Worker Skill 指令。预算键必须是已知角色，新增值必须是整数；运行时执行 `max(256, min(value, default_token_budget * 4))`，旧数据中的非法值回退到默认预算。

新候选先经过严格的字段、类型、大小和确定性指令检查：每个共享或角色策略最多 20 条 prompt 增量（每条 2000 字符）、10 个少样本示例（每个 input/output 4000 字符）、每个策略规则字段 20 条规则，规范化 JSON 最多 64 KiB。检查覆盖危险指令和要求修改生产源码的指令；这是有明确规则边界的校验，不保证识别任意自然语言攻击。生成器候选不合法时不会创建版本；旧存储策略使用宽松加载，避免新规则使历史版本无法加载或回滚。

配置模型的生产服务用相同的模型、内置角色提示词、Agent Skills、确定性扫描器和默认运行限制，对当前完整策略与候选完整策略分别回放验证集和留出集；两次回放的策略可以各自包含角色预算覆盖。回放复用实际审查流程：

```text
Lead 分工 → Security / Correctness-Reliability Workers
         → Lead 评估及最多一轮返工 → Critic 盲审
         → Lead 最终选择 → FindingGate → 预期 finding 匹配
```

运行记录以 `full-agent-policy-replay` 标识该模式。只有未配置 `policy_reviewer_factory` 的旧调用方才使用 `legacy-single-reviewer`；已配置的策略工厂或角色、工具执行失败会计入评测错误并阻止激活，不会自动回退到旧回放路径。候选必须达到配置的验证集最小分数提升，保护指标的验证集退化不得超过允许值，留出集也必须通过非退化门禁；留出集只返回汇总指标，案例详情被脱敏。

当前模型根因生成路径通过门禁后返回 `shadow_ready`，等待影子/灰度审批；这一步没有完成后台影子执行或自动晋级。未配置模型或数据不足时仍可返回 `deferred`。仅当该次进化结果为 `activated`，才把其使用的失败案例标记为已解决；`rejected`、`deferred` 和 `shadow_ready` 都保留未解决状态。

一个现有的 `skill_versions` 版本就是全局 prompt 与结构化策略的完整原子包：`skill_versions.prompt` 保存全局文字，对应 `evolution_runs.metrics.structured_candidate.candidate` 保存策略。完整策略先持久化，再切换生效版本；激活、回滚及 `POST /v1/skills/reload` 均按该版本加载匹配策略，不受评测记录列表分页窗口影响。旧版本没有结构化记录时加载空策略。现有路由、请求格式和存储版本继续兼容，SQLite / PostgreSQL 均无需数据库迁移；运行记录保存策略哈希、评测模式及数据指纹用于审计。

## 架构

```text
HTTP / GitHub Webhook
        │
        ▼
 ReviewService ── TaskStore(SQLite / PostgreSQL)
        │
        ▼
        ReviewHarness (EvoAgent Runtime / checkpoint / resume / budget / trace)
        │
        ├── DiffParser
        ├── Redis Streams / ACK / lease / retry / DLQ
        └── Agentic Lead/Workers
              ├── Lead：一次任务分配并判定风险；高风险时最多请求一轮返工；最终综合
              ├── Security Worker：输入/权限/敏感数据/危险调用链
              ├── Correctness/Reliability Worker：状态/异常/并发/资源/兼容性
              ├── Critic Worker：一次性盲审、反例与证据挑战
              └── Gates：格式、证据、置信度和发布门禁
```

## Skills 的定位与编写方式

`skills/` 下的目录是 Agent Skill 包，不是工具连接器目录。它们目前以审查领域为中心，是因为本项目的产品目标是代码审查；Skill 的职责是把某个领域的判断标准、调查步骤和输出约束注入模型，而不是自己执行 SQL、调用 GitHub 或创建网络连接。

一个 Skill 包至少包含一个 `SKILL.md`：

```text
skills/<skill-name>/
└── SKILL.md                 # YAML 元数据 + 完整运行指令
```

`SKILL.md` 的 frontmatter 由运行时读取：

- `name`：必须与目录名一致，且只使用小写字母、数字和连字符；
- `description`：只用于目录发现和路由，应该短而明确地说明何时启用；
- `allowed-tools`：该 Skill 允许 worker 使用的工具名集合。它是权限声明，不会安装或连接工具；最终权限还会与角色权限取交集；

frontmatter 后的 Markdown 正文才是 Skill 的核心。正文会在 Lead 选中 Skill 后注入对应 worker 的上下文，因此应写成可执行的审查协议，而不是几条标签式提示词。推荐至少包含：任务边界、适用/不适用范围、逐步调查流程、边界条件清单、证据与严重性标准、误报排除规则、修复与验证要求，以及稳定的输出字段。

Skill 可以附带文本资源（例如规范摘录、查询模板或测试清单）。运行时会把资源列入 Skill 包，并仅为已选中的 Skill 注册 `read_skill_resource`；资源不能通过路径穿越或符号链接逃逸出包目录。真正的工具连接由运行时的 `RepositoryToolSuite`、外部集成或插件提供，Skill 只声明“可以使用哪些已经存在的工具”。

运行时流程如下：

```text
磁盘扫描 SKILL.md
        ↓ 只暴露 name/description 给 Lead
Lead 选择相关 Skill
        ↓ 注入 instructions，并收窄 allowed-tools
Worker 使用仓库工具取证
        ↓
Finding gate 校验格式、证据、置信度和发布条件
```

因此，把 Skill 内容直接硬编码到全局上下文并不能完全等价：那会让每个任务都携带全部领域规则，增加上下文长度和相互干扰；Skill 机制可以按任务选择、限制工具权限、热加载版本，并通过 `/v1/skill-evolution/*` 做候选评测、门禁和回滚。对于只有一两句、没有独立选择价值的规则，直接放在全局 prompt 更合适；只有当一组规则有清晰边界、需要按场景启用或需要独立演进时，才应该做成 Skill。

本仓库的内置 Skill 已按上述结构补充了完整的 Mission、Review procedure、Checklist、Evidence/severity、Remediation/verification 和 Output contract。新增 Skill 时可复制其中一个目录，再根据领域替换这些章节，并用 `POST /v1/skills/reload` 让新任务加载最新版本。
