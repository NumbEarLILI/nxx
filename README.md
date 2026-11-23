# conductor

把多个 agent 工具（opencode、Claude Code、Codex、Gemini CLI……）组织成一个常驻的协同系统；敏感任务只交给运行在可信区、连接公司私有模型服务的 agent，且可信区的结果只进不出。

设计说明见 [docs/design.md](docs/design.md)。

## 快速开始

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```

### 1. 启动编排服务

```bash
export CONDUCTOR_ADMIN_TOKEN=... CONDUCTOR_TRUSTED_ZONE_TOKEN=... CONDUCTOR_GENERAL_ZONE_TOKEN=... CONDUCTOR_HOOK_SECRET=...
conductor serve --config examples/server.yaml --host 0.0.0.0 --port 8700
```

### 2. 在各区域启动执行器

可信区主机（网络出口只放行编排服务和私有模型服务）：

```bash
export CONDUCTOR_TRUSTED_ZONE_TOKEN=... COMPANY_LLM_KEY=...
conductor runner --config examples/runner-trusted.yaml
```

普通区主机：

```bash
export CONDUCTOR_GENERAL_ZONE_TOKEN=... ANTHROPIC_API_KEY=... OPENAI_API_KEY=...
conductor runner --config examples/runner-general.yaml
```

### 3. 提交工作流

```bash
export CONDUCTOR_ADMIN_TOKEN=... CONDUCTOR_SERVER=http://conductor.internal.example.com:8700
conductor submit examples/workflow-payment.yaml --wait
conductor status <workflow-id>
```

也可以通过 webhook 触发，见 `examples/server.yaml` 中的 `triggers`。

## 示例文件

| 文件 | 说明 |
| --- | --- |
| `examples/server.yaml` | 区域、agent、分类规则、webhook 触发器 |
| `examples/runner-trusted.yaml` | 可信区执行器（opencode + 私有模型） |
| `examples/runner-general.yaml` | 普通区执行器（Claude Code、Codex、Gemini） |
| `examples/opencode-private.json` | 只允许使用公司私有模型服务的 opencode 配置 |
| `examples/workflow-payment.yaml` | 普通任务与敏感任务混合的工作流 |
