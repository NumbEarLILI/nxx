# 多 Agent 协同系统设计（含敏感任务隔离）

## 1. 目标、约束与已确认的决策

**目标**：把 opencode、Claude Code、Codex CLI、Gemini CLI、Aider 等现有 agent 工具组织起来，按任务拆分、并行、串联地协同工作，且不改造这些工具本身。

**硬约束**：敏感任务只能交给指定的可信 agent；敏感数据及其派生结果不能流向其他 agent。

**已确认的决策**

| 问题 | 决策 | 对设计的影响 |
| --- | --- | --- |
| 可信的依据 | 公司私有部署的模型服务 | 可信与否由“数据最终流向哪个模型服务”决定；可信区网络出口只放行该服务，agent 的模型配置由执行器固定 |
| 敏感结果是否回流 | 不需要 | 可信区**只进不出（sealed）**：普通产物可以流入，可信区的产物、日志、摘要都不出区；不需要降级（declassify）流程 |
| 任务类型 | 代码与非代码都有 | 工作区支持 git 仓库、普通目录、空目录三种；产物统一为输出目录中的文件，git 工作区额外产出 diff |
| 运行形态 | 常驻服务 | 编排服务常驻，提供 HTTP API 与 webhook；每个区域运行常驻执行器，主动拉取任务 |

核心原则：**隔离由编排层和部署结构强制执行，不依赖提示词让 agent “自觉”。**

## 2. 总体架构

```
  用户 / CLI / webhook (GitHub、GitLab、飞书……)
                 │ HTTPS
        ┌────────▼─────────────────────────────────────────┐
        │ 编排服务 conductor serve（不含任何 LLM）             │
        │  工作流 DAG · 敏感分类 · 策略检查 · 调度 · 租约与重试  │
        │  普通产物仓库 · 事件日志 · 审计日志 (SQLite)          │
        └───────▲───────────────────────────────▲──────────┘
                │ 拉取任务 / 上报状态             │ 拉取任务 / 只上报状态
                │ 上传产物与日志                  │ （产物、日志、摘要不出区）
   ┌────────────┴───────────┐        ┌──────────┴──────────────────────┐
   │ 普通区执行器 (general)   │        │ 可信区执行器 (trusted, sealed)     │
   │ claude-code / codex /   │        │ opencode → 公司私有模型服务        │
   │ gemini …                │        │ 区内产物仓库 · 本地日志            │
   │ 工作区剔除敏感路径        │        │ 出口只放行编排服务与私有模型服务     │
   └─────────────────────────┘        └─────────────────────────────────┘
```

- **拉取模型**：执行器主动连接编排服务认领任务，编排服务不需要能访问可信网络，可信区只需开放一条到编排服务的出站连接。
- **区域（zone）是信任边界**：每个区域一个令牌、一个权限级别（clearance）、是否 sealed。agent 归属于区域，执行器只能替本区域的 agent 认领任务。
- **编排服务不含 LLM**：它只做确定性的规则判断，因此不会把任务内容发给任何模型服务。

## 3. Agent 接入

执行器把每个 agent 当作一个命令行程序调用，配置一条命令模板即可接入：

```yaml
agents:
  opencode-private:
    command: ["opencode", "run", "--model", "company/qwen-coder", "{prompt}"]
    env: { OPENCODE_CONFIG: /etc/conductor/opencode-private.json }
  claude-code:
    command: ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits"]
  codex:
    command: ["codex", "exec", "--full-auto", "{prompt}"]
```

可用占位符：`{prompt}`、`{prompt_file}`、`{workspace}`、`{inputs_dir}`、`{output_dir}`；同时注入环境变量 `CONDUCTOR_WORKSPACE`、`CONDUCTOR_INPUTS_DIR`、`CONDUCTOR_OUTPUT_DIR` 等。

执行器为每次运行生成提示词，说明任务、工作目录、上游产物位置、交付物输出位置和验收命令。agent 的环境变量**从零构建**（只透传 `inherit_env` 白名单和该 agent 的 `env`），执行器自己的区域令牌不会进入 agent 进程。

后续可在同一接口下增加 ACP（`opencode acp` 等）适配，获得结构化事件和权限请求，见第 9 节。

## 4. 安全模型

### 4.1 级别

```
L0 public < L1 internal < L2 confidential < L3 restricted
```

- **任务级别**：用户声明，加上分类规则自动判定——路径规则（`paths` 字段匹配 `payment/signing/**` 等）和内容规则（内置私钥、AWS key、`password=…`、身份证号、手机号检测，可自定义正则）。**只能上调，不能下调。**
- **级别沿数据传播**：任务读取了哪些上游任务的产物（`inputs`，默认等于 `depends_on`），级别就至少是这些上游的最高级别。只有顺序依赖、不读产物时（`inputs: []`）不传播。
- **区域权限**：取决于“工具 + 模型服务 + 运行环境”的整体。opencode 接公司私有模型服务且运行在受控网络中，才是 L3；同样的 opencode 接公共 API 只能算普通区。

### 4.2 强制规则（编排服务实现）

1. **派发**：任务只派给 `clearance ≥ 任务级别` 且具备所需能力的 agent。没有合适的 agent 就排队，绝不降级派发。
2. **读取**：执行器下载上游产物时，要求该产物确实是本任务的输入，且 `clearance ≥ 产物标签`；允许和拒绝都写审计日志。
3. **只进不出**：sealed 区域上传产物、上报日志事件、上报摘要或错误文本，编排服务一律拒绝（403）。sealed 区域只能上报“成功/失败 + 退出码”。
4. **区域固定**：读取 sealed 任务产物的下游任务，只能在同一个 sealed 区域执行（产物只存在于该区域的本地仓库）。
5. **可信容量保护**：sealed 区域默认只接普通区处理不了的任务（级别高于所有普通区的 clearance），避免普通任务被可信 agent 抢走、产物被锁在区内、占用稀缺的可信容量。可用 `min_level` 覆盖。

### 4.3 运行时隔离

- **工作区快照**：git 工作区不是克隆，而是从执行器本地缓存 `git archive` 导出、剔除 `workspace_exclude` 路径后重新 `git init`。被剔除的文件既不在文件树里，也不在 agent 可见的 git 历史里。目录工作区同样按规则复制。
- **来源白名单**：`allowed_sources` 限制执行器愿意导出的仓库和目录，防止有人提交任务让普通区导出敏感仓库。
- **网络出口（部署层，必须做）**：可信区主机只允许访问编排服务和公司私有模型服务。这是“只用私有模型”的真正保证；opencode 配置里禁用其他 provider、关闭分享和自动更新是第二道防线（见 `examples/opencode-private.json`）。
- **凭据**：私有模型服务的密钥只存在于可信区主机；普通区执行器拿不到。
- **进程隔离（待做）**：当前 agent 以执行器同一用户运行，能读到该用户可读的文件。生产部署应让每次运行进入独立容器或独立用户，只挂载本次运行目录，见第 9 节。

### 4.4 已知的剩余风险

- **编排服务能看到 L3 任务的标题和描述**。它不含 LLM，不会外发，但应部署在公司内网。更严格的方案是“密封载荷”：提交方用可信区执行器的公钥加密 L3 任务正文，编排服务只存密文和元数据（第 9 节）。
- **提示词注入**：webhook 触发的任务正文来自外部（如 issue 内容）。普通区 agent 被注入后能做的事，受限于它本来就只能看到的普通数据；可信区不应由外部 webhook 直接触发 L3 任务而不经人工确认。
- **分类是兜底而非保证**：路径和内容规则会漏判。敏感仓库应通过 `allowed_sources` 和 `workspace_exclude` 从结构上隔离，而不是依赖分类。

## 5. 任务模型与协作

### 5.1 工作流定义

```yaml
name: add-payment-method
tasks:
  - id: contract
    title: 定义新支付方式的 HTTP 接口契约
    requires: [code]
    workspace: { type: git, source: /srv/repos/app }
  - id: signing
    title: 实现回调签名校验与密钥加载
    requires: [code, security]
    paths: [payment/signing/verify.ts]       # 命中路径规则 → L3
    depends_on: [contract]                    # 读取 contract 的产物（L0 流入可信区，允许）
    workspace: { type: git, source: /srv/repos/app }
    acceptance: ["pnpm -s test payment/signing"]
  - id: signing-review
    title: 评审签名实现的安全性
    requires: [review]
    depends_on: [signing]                     # 读取 L3 产物 → 自动 L3，固定在可信区
```

字段：`level`、`requires`（能力）、`depends_on`、`inputs`、`paths`、`workspace`（`none` / `git` / `dir`）、`acceptance`（在工作区执行的验收命令）、`max_attempts`。

### 5.2 协作模式

- **流水线**：设计 → 实现 → 测试 → 评审，每步可由不同 agent 完成。
- **并行扇出**：互不依赖的任务同时派发给不同 agent。
- **混合级别**：同一需求中，普通部分在普通区并行，敏感部分在可信区执行，普通产物（如接口契约）作为两边的共同输入。
- **代码与非代码统一**：文档、调研、数据分析任务用 `none` 或 `dir` 工作区，交付物写入输出目录；代码任务用 `git` 工作区，diff 自动收集为 `changes.diff`。

### 5.3 产物

- 普通区：产物上传到编排服务，标签等于任务级别，下游按读取规则下载。
- sealed 区：产物写入区内本地仓库（`data_dir/artifacts/<工作流>/<任务>/`），下游在同区内直接读取。多台可信执行器时该目录应放在区内共享存储上。
- 敏感结果的最终交付（合并到受保护分支、写回内部系统）在可信区内完成，不经过编排服务。

## 6. 调度与可靠性

1. 执行器带着各 agent 的空闲槽位认领；编排服务按工作流提交顺序遍历就绪任务（依赖全部成功），选第一个满足第 4.2 节规则的 agent。
2. 认领时发放租约，执行器周期性心跳续约；租约过期则任务重新排队（超过 `max_attempts` 判失败）。
3. 失败自动重试；最终失败时，下游任务标记为 `skipped`，工作流标记为失败。
4. 每个 agent 的并发由服务端 `max_concurrency` 与执行器 `slots` 共同限制。

## 7. 常驻服务接口

| 接口 | 说明 |
| --- | --- |
| `POST /api/workflows` | 提交工作流（JSON 或 YAML），返回每个任务的级别及判定原因 |
| `GET /api/workflows[/{id}]` | 查询状态；sealed 任务只有状态和退出码 |
| `POST /api/workflows/{id}/cancel` | 取消 |
| `GET /api/workflows/{id}/tasks/{tid}/events` | 普通任务的运行日志 |
| `GET /api/workflows/{id}/tasks/{tid}/artifacts/{name}` | 下载普通产物 |
| `GET /api/audit` | 审计日志：提交、分类结果、派发、产物读取与拒绝、完成、租约过期 |
| `POST /api/hooks/{name}` | webhook 触发：支持 GitHub `X-Hub-Signature-256` 签名或 `X-Conductor-Token`；`when` 过滤，`{{payload.x}}` 模板生成工作流 |
| `/api/runner/*` | 执行器协议：认领、心跳、上传产物、完成、读取输入 |

## 8. 实现现状（`src/conductor`）

| 模块 | 内容 |
| --- | --- |
| `models.py` | 服务配置、工作流定义与校验（重复 ID、未知依赖、环） |
| `policy.py` | 分类规则、级别传播、派发判定 |
| `engine.py` | 状态机、调度、租约、重试、sealed 规则、审计 |
| `server.py` | FastAPI 服务与鉴权 |
| `triggers.py` | webhook 验签、过滤与模板 |
| `workspace.py` | git 快照导出、目录复制、来源白名单、diff 收集 |
| `runner.py` | 区域执行器：认领、准备工作区、拉取输入、调用 agent、验收、收集产物、上报 |
| `cli.py` | `conductor serve / runner / submit / status` |

测试（`tests/`）用一个假 agent 覆盖端到端流程，包括：敏感任务只进可信区、sealed 区上传被拒、下游固定、跨级别读取被拒并审计、被剔除文件不在 git 历史中、执行器令牌不进入 agent 环境、重试与跳过、租约过期、webhook。

## 9. 后续阶段

**阶段二：协同增强**
- 作者-评审循环：评审意见作为作者下一轮输入，最多 N 轮。
- 多方案择优：同一任务派给多个 agent，按验收结果或评审打分选优。
- 编排 MCP 服务：让 agent 在运行中提交子任务、读取允许的产物（同样经过读取规则）。
- 简单 Web 看板：工作流 DAG、运行日志、审计查询。

**阶段三：加固**
- 每次运行进入独立容器（只挂载运行目录，按区域设置网络策略）。
- 密封载荷：L3 任务正文用可信区公钥加密，编排服务只存密文。
- 可信区内的人工确认点：外部触发的 L3 任务、合并到受保护分支前。
- ACP 适配器：结构化事件、细粒度工具权限。
- 可信区内的规划 agent：在区内把敏感需求拆成子任务，子任务同样只在区内执行。
