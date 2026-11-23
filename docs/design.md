# 多 Agent 协同系统设计（含敏感任务隔离）

## 1. 目标与约束

**目标**

- 把 opencode、Claude Code、Codex CLI、Gemini CLI、Aider 等现有 agent 工具组织起来，按任务拆分、并行、串联、互审地协同工作。
- 不改造这些工具本身，只通过它们已有的接口（非交互 CLI、ACP、MCP）接入。

**硬约束**

- 敏感任务只能交给指定的可信 agent 执行。
- 敏感数据（源码片段、密钥、日志、客户数据……）以及从敏感数据派生出来的结果，不能流向非可信 agent，包括不能经由“编排器自己的 LLM”或“共享上下文”间接泄漏。

这条约束决定了整个架构：**隔离必须在编排层强制执行，不能靠提示词让 agent“自觉”。**

## 2. 总体架构

```
                    ┌──────────────────────────────────────────┐
                    │              控制面 (Orchestrator)          │
 用户 / CLI / Web ─▶ │  任务 DAG 引擎 · 策略引擎 · 调度器 · 审计日志  │
                    │  人工审批队列 · 产物仓库(带标签与 ACL)        │
                    └───────┬─────────────────────┬──────────────┘
                            │ 统一 Adapter 接口       │ MCP 工具（agent 反向调用）
          ┌─────────────────┼─────────────────┐   │  submit_artifact / ask_peer /
          ▼                 ▼                 ▼   │  request_subtask / read_artifact
   ┌────────────┐    ┌────────────┐    ┌──────────────────────┐
   │ 普通区 Zone  │    │ 普通区 Zone  │    │ 可信区 Trusted Zone     │
   │ Claude Code │    │ Codex CLI  │    │ opencode + 本地/私有模型 │
   │ 容器+worktree│    │ 容器+worktree│    │ 独立机器/容器，独立凭据   │
   └────────────┘    └────────────┘    └──────────────────────┘
```

核心组件：

| 组件 | 职责 |
| --- | --- |
| 任务 DAG 引擎 | 保存任务、依赖、状态；驱动执行 |
| 策略引擎 | 根据敏感级别决定“谁能执行、谁能读结果” |
| 调度器 | 在满足策略的 agent 中按能力、负载、成本选择 |
| Adapter 层 | 把各种 agent 工具包装成统一接口 |
| 产物仓库 | 存放任务输出（文本、diff、文件），每个产物带安全标签 |
| 编排 MCP Server | 暴露给 agent 的工具，让 agent 能提交结果、请求子任务、向其他 agent 提问——所有调用都过策略引擎 |
| 审计日志 | 记录每次派发、每次产物读取、每次降级（declassify） |

## 3. Agent 接入：统一 Adapter

### 3.1 接口

```ts
interface AgentAdapter {
  id: string;                      // "opencode-trusted"
  capabilities: string[];          // ["code", "refactor", "review", "shell"]
  clearance: Level;                // 该 agent 允许处理的最高敏感级别
  run(task: TaskSpec, ws: Workspace, signal: AbortSignal): AsyncIterable<AgentEvent>;
  cancel(runId: string): Promise<void>;
}

type AgentEvent =
  | { type: "log"; text: string }
  | { type: "tool_call"; name: string; args: unknown }
  | { type: "artifact"; path: string; kind: "diff" | "file" | "report" }
  | { type: "need_input"; question: string }       // 需要人或其他 agent 回答
  | { type: "done"; summary: string; exitCode: number };
```

### 3.2 三种接入方式（按优先级）

1. **ACP（Agent Client Protocol）**：opencode（`opencode acp`）、Gemini CLI 等已支持，基于 stdio 的 JSON-RPC，能拿到结构化的流式事件、工具调用、权限请求。首选。
2. **非交互 CLI**：`opencode run "..."`、`claude -p ... --output-format stream-json`、`codex exec ...`、`gemini -p ...`、`aider --message ...`。Adapter 负责拼命令、解析输出、收集工作目录里的 diff。
3. **HTTP/SDK**：工具自带 server 模式时（如 `opencode serve`）直接调 API。

每个 agent 只需要写一个几十到一两百行的 Adapter；新工具接入不影响其他部分。

### 3.3 反向通道：编排 MCP Server

给每个 agent 挂载同一个 MCP Server（按 agent 身份鉴权），提供：

- `read_artifact(id)`：读取上游产物，**策略引擎检查读者 clearance ≥ 产物标签**。
- `submit_artifact(...)`：提交结果，自动继承标签。
- `request_subtask(description, required_capability)`：agent 自己拆出子任务交回编排器调度。
- `ask_peer(question, target_capability)`：向其他 agent 提问（同样过策略）。

这样 agent 之间“协同”永远经过编排器中转，不存在点对点直连，隔离才可控。

## 4. 安全模型（核心）

### 4.1 敏感级别与标签

```
L0 public < L1 internal < L2 confidential < L3 restricted
```

- **任务**有 `level`：由用户显式标注，或由分类规则自动判定（路径规则如 `secrets/**`、`infra/prod/**`；内容规则如密钥正则、PII 检测）。自动判定只能**上调**级别，不能下调。
- **Agent** 有 `clearance`：取决于 *工具 + 模型端点 + 运行环境* 三者整体，而不是工具名。例如“opencode + 本地 Ollama + 断网容器”可以是 L3；“opencode + 某云 API”只能算 L1。这一点很容易被忽略：可信的其实是数据最终流向的模型服务。
- **产物**有 `label`：等于产生它的任务级别与它读过的所有输入标签的最大值（污点传播）。

### 4.2 三条强制规则

1. **派发规则**：任务只能派给 `clearance ≥ task.level` 的 agent。没有满足条件的 agent 时任务进入等待/人工处理，绝不降级派发。
2. **读取规则**：agent 读取产物（`read_artifact`、挂载的文件、注入的上下文）时要求 `clearance ≥ artifact.label`。
3. **降级规则**：高标签产物要流向低 clearance 的 agent，必须经过显式的**降级（declassify）步骤**：由可信 agent 生成脱敏摘要 + 规则扫描（密钥/PII）+ 人工审批（L3 必须人工），降级动作写入审计日志。

### 4.3 编排器自身的 LLM 也是一个“读者”

如果用 LLM 做任务规划（拆解 DAG），它会看到任务内容，因此：

- 规划器本身也要有 clearance，按同样规则检查。
- 推荐做法：普通规划器只看到敏感任务的**元信息**（“对 `payment/` 模块做安全修复，L3”），具体拆解交给可信 agent 在可信区内完成，回传的子任务列表也带 L3 标签。
- 或者干脆让可信 agent 兼任规划器（如果它的能力够）。

### 4.4 运行时隔离（纵深防御）

策略引擎是逻辑隔离，还需要物理隔离兜底：

- **工作区**：每次运行一个独立 git worktree / 目录；敏感路径通过稀疏检出（sparse checkout）根本不进入普通区的工作区。
- **容器/沙箱**：每个 agent 跑在独立容器中；可信区放在独立主机或独立网络段。
- **网络出口**：可信区只允许访问其模型端点（或完全断网 + 本地模型）；普通区禁止访问可信区和产物仓库的高标签数据。
- **凭据**：敏感凭据只注入可信区容器，普通区拿不到。
- **合并**：敏感任务产出的代码 diff 由编排器在可信区内合并到受保护分支，不经过普通 agent 的工作区。

## 5. 任务模型与协作模式

### 5.1 任务定义

```yaml
id: t-42
title: 修复支付回调的签名校验
level: L3                       # 显式标注，或由分类规则推导
requires: [code, security]
inputs: [artifact://a-17]       # 上游产物
depends_on: [t-40]
review:
  by_capability: review
  min_reviewers: 1
acceptance:
  - cmd: "pnpm test payment"
```

### 5.2 常用协作模式（都用 DAG 表达）

- **流水线**：设计 → 实现 → 测试 → 评审，每步可以是不同 agent。
- **并行扇出**：一个大需求拆成多个互不相交的模块，分给多个 agent 并行，最后合并。
- **作者-评审**：A 写、B 审，B 的意见作为 A 的下一轮输入，最多 N 轮。敏感任务的评审者也必须满足 clearance。
- **多方案择优（best-of-N）**：同一任务派给多个 agent，用测试结果或评审 agent 打分选最优。
- **混合级别拆分**：一个需求里只有一部分敏感，例如“新增支付方式”：UI 和文档（L1）交给普通 agent，签名与密钥处理（L3）交给可信 agent，接口契约（L1，由人或可信 agent 降级发布）作为两边的共同输入。这是最能体现系统价值的模式。

### 5.3 共享状态

- 代码协作以 **git** 为载体：每个任务一个分支，编排器负责合并与冲突处理（冲突时派发“解决冲突”子任务）。
- 非代码信息（设计说明、评审意见、测试报告）进入**产物仓库**，带标签与 ACL，不用全局共享的“黑板”或聊天记录，避免标签失控。

## 6. 调度

1. 过滤：`clearance ≥ level` 且 `capabilities ⊇ requires`。
2. 排序：能力匹配度、历史成功率（按任务类型统计）、当前负载、成本。
3. 并发控制：每个 agent 设置最大并发；可信 agent 通常只有一个，敏感任务排队，必要时允许多实例同配置的可信 agent 横向扩展。
4. 失败处理：超时/失败重试 → 换同级别其他 agent → 进入人工队列。

## 7. 可观测与人机协同

- 实时查看每个运行的事件流（来自 Adapter 的 `AgentEvent`）。
- 审批点：L3 任务派发前（可选）、所有降级操作、合并到受保护分支。
- 审计日志不可篡改（追加写），记录：谁、何时、对哪个任务/产物、做了什么、依据哪条策略。日志本身也按标签控制可见性。

## 8. 技术选型建议

| 部分 | 建议 |
| --- | --- |
| 语言 | TypeScript（Node）或 Python，二者 ACP/MCP SDK 都齐全 |
| 状态存储 | 起步用 SQLite（任务、产物元数据、审计），后续可换 Postgres |
| 产物存储 | 本地文件 + 内容哈希；可信区产物存可信区本地磁盘，编排器只存元数据 |
| 执行隔离 | Docker/Podman；可信区单独主机或单独 rootless 用户 |
| 编排 MCP Server | 官方 MCP SDK，按 agent 发放独立 token |
| 前端 | 起步只做 CLI + 日志；后续加一个看板展示 DAG 与审批 |

一个关键部署选择：**可信区运行一个轻量的“区域执行器（zone runner）”**，编排器只给它下发任务 ID 和元数据，敏感内容不经过编排器主进程。这样即使编排器本身部署在非可信环境，也不会接触 L3 数据。

## 9. 分阶段落地

**阶段一：最小可用**

- 任务 DAG（手写 YAML）+ SQLite 状态。
- 两个 Adapter：opencode（可信，CLI 模式）+ 另一个常用 agent（普通）。
- 派发规则 + 读取规则 + 每任务独立 worktree。
- 审计日志。

**阶段二：协同**

- 编排 MCP Server（`read_artifact` / `submit_artifact` / `request_subtask`）。
- 作者-评审、并行扇出、best-of-N 模式。
- 自动敏感分类（路径 + 密钥/PII 扫描），只上调不下调。

**阶段三：加固与扩展**

- 容器隔离、网络出口策略、可信区 zone runner。
- 降级流程 + 人工审批 UI。
- ACP Adapter 替换 CLI Adapter，获得结构化事件和权限控制。
- LLM 规划器（遵守 4.3 的规则）。

## 10. 需要你确认的问题

这些答案会影响细节，但不影响上述架构：

1. “特定的可信 agent”可信的原因是什么——本地模型、公司私有部署的模型端点，还是运行在特定机器上？这决定 clearance 的定义方式和可信区的部署位置。
2. 敏感任务的结果是否需要回到普通 agent 继续使用？如果需要，降级流程（第 4.2 节第 3 条）是必须的；如果不需要，可以直接把可信区做成单向“只进不出”。
3. 协作对象主要是代码仓库，还是也包括文档、数据分析等非代码任务？
4. 期望的使用方式：命令行一次性跑一个 DAG，还是常驻服务接收任务（如从 issue 或聊天工具触发）？
