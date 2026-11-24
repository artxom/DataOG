# Data.O.G AI 助手升级设计方案：弱模型超长 SQL 增幅器、动态窗口探查与持久化 `/goal` 引擎

> **适用基线**：Data.O.G `v3.3.1`（`core/platform/llm_hub.py`、`modules/ai_assistant/`、`static/js/apps/ai_assistant/`）  
> **目标场景**：在使用 `deepseek-v4-flash` 等轻量/快速/较早代模型，且存在 **几千行复杂 SQL / 存储过程分析** 以及 **上游供应商响应较慢或偶发超时** 的生产环境下，实现高准确率、可断点续跑、**“不达目的不结束”** 的全链路分析体验。  
> **工程红线**：延续零第三方运行依赖（`requirements.txt` 保持不变）、Layer 2 私有库严格隔离（零泄露至 `.dataog.db`）、零 Cross-Module 私有互引、前端零 `innerHTML`/`outerHTML`/`insertAdjacentHTML` 与零阻塞弹窗。

---

## 一、现状诊断与核心痛点

结合 `v3.3.1` 现行实现，制约轻量/古早模型分析超长 SQL 的三大结构性瓶颈如下：

1. **「标称窗口 ≠ 有效推理注意力窗口」导致的中间失忆（Lost-in-the-Middle）**：
   - 当前 [llm_hub.py](../core/platform/llm_hub.py) 中 `infer_model_capabilities()` 仅凭字符串前缀将所有含 `deepseek` 的模型一律按 `context_window=128_000`、`attachment_budget=80_000` 处理，既未动态探查远端服务商真实配额/速率/token 比率，也未区分旗舰模型与 `-flash`/`-mini`/`-lite`/古早模型。
   - 轻量模型虽然物理上可能接收 3~8 万 tokens，但在超过 **15K~25K tokens** 的高密度存储过程上极易丢失中段临时表定义、遗漏后半段 `EXCEPTION`/`UPDATE` 分支或臆造字段名。
2. **纯文本按行数切分缺乏 SQL 语义骨架与数据血缘视图**：
   - `attachments.py` 的 `extract_attachment_outline()` 仅提取对象名、注释章节与扁平表清单，没有形成「语句级 CRUD 血缘图 + 控制流嵌套 + 静态隐患体检」，也缺乏按血缘依赖精准切片的工具，弱模型仍需耗费宝贵步数自行大海捞针。
3. **单次 HTTP 请求绑定的同步超时脆弱性，缺乏 `/goal` 级持续状态机**：
   - 目前 `/api/modules/ai_assistant/chat` 与 `/chat/stream` 将整轮探索强绑定在单次 HTTP 连接上（`LOOP_WALLCLOCK_BUDGET_SEC = 300.0`，单步默认 HTTP 读超时 `60s`/`120s`，`MAX_STEPS_CAP = 10`）。一旦供应商思考或响应超过单次窗口、遇到 `429/502/504` 抖动或客户端切换标签/断连，整轮进度前功尽弃，且缺乏「目标未达成则自动补救直至验收通过」的闭环。

---

## 二、业界成熟实践借鉴与四大核心支柱架构

```text
用户上传超长存储过程 + 发起目标 (标准模式 / 🎯 Goal 深度模式)
   │
   ▼ [Phase A: 动态模型探查 (Model Capability Probe)]
LLMHub.probe_model_capabilities()
   ├─ L1 元数据：/models 上下文字段 & Ollama /api/show（0 成本）
   ├─ L2 活体探针：按需 max_tokens=32 测真实 TTFT、token_ratio、Thinking/SSE 兼容度
   ├─ L3 错后自校准：400 ContextLengthExceededError 正则回写实际窗口上限
   └─ 输出分级档案：model_tier (flagship|standard|lightweight) + effective_reasoning_budget + recommended_chunk_tokens
   │
   ▼ [Phase B: 本地确定性 SQL 静态解剖与血缘建模 (0 Token)]
sql_static_analyzer.py
   ├─ 语句索引 (statements: stmt_id, verb, start_line..end_line, target, sources, cte, tokens)
   ├─ 血缘 DAG (lineage_dag: ODS -> TMP临时表 -> DWD/DM目标表)
   ├─ 控制流骨架 (DECLARE/CURSOR/LOOP/EXECUTE IMMEDIATE/EXCEPTION/COMMIT)
   └─ 静态体检 (LOOP_DML / UNBOUNDED_DML / SELECT_STAR / DYNAMIC_SQL / MID_COMMIT / 方言兼容雷区)
   │
   ▼ [Phase C: 自适应执行规划器 (ExecutionPlanner)]
综合 [模型真实窗口 + model_tier] × [SQL 总规模 + 血缘复杂度] × [用户意图: 局部排查 vs 全局审计]
   └─ 选定最优方案: full_context | lineage_sliced | goal_driven_staged | agentic_explore | stepped_batch
      (通过 SSE/RunEvent 推送 `plan_ready` 规划卡片展示给用户)
   │
   ▼ [Phase D: 持久化后台 Run + Goal 状态机 + 抗超时韧性执行]
probing ─► planning (阶段Checklist) ─► running (甜密区单阶段 + 手术刀切片 + 压缩视图)
   ▲                                        │
   │ 缺漏/未通过验收？自动生成补救子任务        ▼
   └─────────── verifying (硬覆盖率 & 结论核验) ──► completed (全景综合 + length 自动续写)
                        │
                        └─ 超步数/断网 ─► paused_recoverable (SQLite Checkpoint 一键断点续跑)
```

---

### 支柱一：动态模型探查器 (`ModelProbe`) 与自适应执行规划器 (`ExecutionPlanner`)

#### 1. 四级分层探查体系 (`core/platform/llm_hub.py`)
在执行复杂分析或上传超长 SQL 前，先探查当前选用模型（含旧版/兼容网关别名模型如 `deepseek-v4-flash`），不盲信硬编码模型名表：

1. **L1 · 零额外开销远端元数据抽取**：
   - 升级 `LLMHub.fetch_models()` 与 `_http_fallback_models()`，解析并保留网关 `/models` 返回的完整元数据字段：
     - OpenAI/vLLM/OneAPI/OpenRouter/DeepSeek/硅基流动等常见扩展字段：`context_length`、`max_model_len`、`context_window`、`input_token_limit`、`max_input_tokens`、`output_token_limit`、`max_output_tokens`、`capabilities`（`reasoning`/`thinking`）；
     - 若检测到本地 Ollama 端点（`127.0.0.1:11434` 或 `localhost:11434`），额外按需请求 `POST /api/show` 解析 `num_ctx` 与参数规模。
2. **L2 · 轻量流式活体探针 (`LLMHub.probe_model_capabilities`)**：
   - 在缓存缺失、过期（默认 TTL `24h`）或用户点击「探查模型窗口与延迟」时触发一次极低成本请求（`max_tokens=32`，含固定标定 SQL 短文本，`stream=True`）：
     - **实测 TTFT（首字延迟 ms）与流式吞吐（tok/s）**：用于动态计算后续超长任务的合理连接/静默超时阈值；
     - **真实 Token 换算校准系数 (`token_ratio`)**：对比客户端 `estimate_tokens(probe_text)` 与服务端返回 `usage.prompt_tokens`，修正中英混杂 SQL 场景的 Token 估算偏差；
     - **Thinking 与 SSE 兼容性探测**：识别是否返回 `reasoning_content` / `<think>`、是否支持 `thinking` / `reasoning_effort`、是否真正流式下发。
   - **安全原则**：探针失败或超时 **绝不阻断** 主对话流程，自动优雅降级至静态启发规则。
3. **L3 · 运行期 400 超限错误零浪费自校准**：
   - 新增正则解析上游 `ContextLengthExceededError` 报错文本中的真实数字（如 `"maximum context length is 32768 tokens"`、`"limit 65536"`、`"(32k)"` 等），当即回写持久化探针缓存，使下一轮重试与后续会话立刻采用准确的窗口上限，不再反复撞墙。
4. **L4 · 区分「物理窗口 (`context_window`)」与「有效推理预算 (`effective_reasoning_budget`)」**：
   - 在能力对象中新增以下字段：
     - `model_tier`：`"flagship"` | `"standard"` | `"lightweight"`。凡模型名匹配 `-flash` / `-mini` / `-lite` / `-fast` / `-nano` / `-tiny` / `-haiku` / `-turbo`（除旗舰大窗外）或古早/小参数本地模型（如 `deepseek-v4-flash`），自动定档为 **`lightweight`**（支持人工在高级设置覆盖）；
     - `effective_reasoning_budget`：`lightweight` 模型即便物理窗口标称 64K/128K，其单轮**高准确率推理甜蜜区**默认收敛在 `16_000 ~ 24_000 tokens`（总输入含系统提示），附件单次直通阈值相应收紧至 `min(attachment_budget, effective_reasoning_budget)`；
     - `recommended_chunk_tokens`：针对不同 `model_tier` 下发最合适的分段目标粒度（`flagship` 保持 `12_000`、`standard` `8_000`、`lightweight` 自动收窄至 `5_000 ~ 6_000 tokens` 按完整 SQL 语句/章节边界切分）。

#### 2. 探针缓存与 API 暴露
- **持久化位置**：存入 Layer 2 私有库 `llm_providers.extra_json["model_probes"][model]`（严禁进 `.dataog.db` 共享工作区），结构如下：
  ```json
  {
    "context_window": 64000,
    "effective_reasoning_budget": 18000,
    "max_output_tokens": 8192,
    "recommended_chunk_tokens": 5500,
    "model_tier": "lightweight",
    "supports_thinking": false,
    "token_ratio": 1.08,
    "ttft_ms": 1420.5,
    "stream_supported": true,
    "source": "live_probe | models_meta | error_calibrated | heuristic | custom",
    "probed_at": "2026-11-24T21:00:00"
  }
  ```
- **新增平台接口 (`core/platform/routes/appearance_and_llm.py`)**：
  - `POST /api/platform/llm/probe-model`（支持 `force_refresh: bool`，返回探查档案并刷新 `list_available_models()`）。

#### 3. 每次执行前的策略规划器 (`ExecutionPlanner`)
在构建模型请求之前，根据 `(模型探针档案, SQL 静态解剖结果, 用户问题意图, 运行模式)` 决策本次执行计划，并下发 `plan_ready` 事件给前端展示透明决策依据：

| 场景条件 | 自动制定策略 | 说明 |
| :--- | :--- | :--- |
| 总估算 tokens `<= effective_reasoning_budget` 且非巨型 CSV | **`full_context`** | 整篇带真实行号一次性直通，零分片、最高连贯性 |
| 用户提问包含明确目标表名/报错阶段/字段，且超过直通预算但血缘切片后 `<= effective_reasoning_budget` | **`lineage_sliced`** | 手术刀式抽出相关血缘子图语句块 + 静态骨架图送入，**高准度且单轮秒回** |
| 用户开启 🎯 **Goal 模式**，或全局审计/全量重构且超过 `effective_reasoning_budget` | **`goal_driven_staged`** | 按静态解剖阶段自动划分为按甜蜜区容量均衡的多个目标阶段（含每阶段语句区间 & 必查静态疑点），由 Goal 状态机逐段推进并校验覆盖率 |
| 常规对话下超大脚本，无强全量遍历诉求 | **`agentic_explore`** | 全景血缘图 + 控制流骨架 + 精简按需切片工具集（`trace_attachment_lineage` / `read_attachment_statement` / `read_attachment_lines` / `search_attachment`） |
| 用户显式指定或超大脚本单批兜底 | **`stepped_batch`** | 按 `recommended_chunk_tokens` 自适应切分 + 滚动账本接续 |

---

### 支柱二：「弱模型增幅器」——确定性 SQL 静态解剖、血缘 DAG 与轻量压减

将 **100% 确定性的代码结构识别与问题初筛** 下沉为纯 Python 词法/状态机引擎，让 `deepseek-v4-flash` 开局站在“专家写好的架构图纸”上分析：

#### 1. 新建本地静态解剖引擎 (`modules/ai_assistant/sql_static_analyzer.py`)
纯标准库实现（复用 `core/platform/sql_risk_guard.py` 中的词法扫描思路，避免正则误拆字符串/注释），产出的 JSON 总量控制在 ~2–4 KB（极省 Token）：

- **`statements`（语句级清单）**：
  - 精确识别存储过程/脚本内每条顶级与内部关键 SQL 的：`stmt_id`（`S001`, `S002`...）、`verb`（`CREATE_PROC` / `CREATE_TEMP_TABLE` / `CTAS` / `INSERT` / `MERGE` / `UPDATE` / `DELETE` / `TRUNCATE` / `EXECUTE_IMMEDIATE` / `SELECT` / `CALL`）、`start_line`、`end_line`、`target_table`、`source_tables`、`ctes`、`in_loop`（是否位于游标/`FOR`/`WHILE` 循环体）、`estimated_tokens`、`section_title`。
- **`lineage_dag`（数据血缘有向无环图）**：
  - 自动归类表节点角色：`source_only`（只读源表）、`temp_staging`（过程中创建/截断/写入又被下游消费的临时表/中间表/`#temp`）、`final_target`（只写最终落标/业务目标表）；
  - 记录每条数据流边：`src -> dst @ S004 (L180-L245)`，并生成紧凑可读的文本摘要直接注入系统上下文，例如：
    ```text
    [血缘流] ods.txns ─(S002 L45-L98 CREATE TEMP)─► tmp_daily_amt ─(S014 L620-L710 INSERT)─► dwd.settle_agg
    ```
- **`control_flow`（控制流与事务边界）**：
  - 提取过程参数与 `DECLARE` 核心变量（含类型与默认值）；
  - 记录游标/`LOOP`/`WHILE` 嵌套起止行号；
  - 记录 `EXECUTE IMMEDIATE` 动态拼接 SQL 行号、`COMMIT` / `ROLLBACK` 行号及 `EXCEPTION WHEN ...` 异常捕获块。
- **`static_findings`（确定性静态隐患雷达）**：
  - **`LOOP_DML` [HIGH]**：循环内部执行逐行 `INSERT`/`UPDATE`/`DELETE`/`SELECT INTO`（性能炸弹）；
  - **`UNBOUNDED_DML` [HIGH]**：缺失 `WHERE` 条件的全表 `UPDATE` / `DELETE`；
  - **`DYNAMIC_SQL` [MEDIUM]**：`EXECUTE IMMEDIATE` 动态 SQL（提示可能存在注入或隐蔽跨表依赖）；
  - **`MID_COMMIT` [MEDIUM]**：过程主干中间出现 `COMMIT`/`ROLLBACK`（提示事务一致性或 DWS/PG 存储过程兼容性隐患）；
  - **`SELECT_STAR` / `DISTINCT_HEAVY` [LOW]**：宽表 `INSERT INTO ... SELECT *` 等规范/性能隐患；
  - **`DIALECT_COMPAT` [MEDIUM]**：结合当前目标方言（如 `DWS` / `PGSQL`）检测遗留语法（Oracle `(+)` 外连接、`ROWNUM`、`NVL`/`DECODE`、`FROM DUAL`、`TRUNC(sysdate)` 等），精确标出 `Lxxx` 行号。

#### 2. 高信噪比「手术刀切片」与语义保真压缩工具
1. **在 `modules/ai_assistant/tools.py` 新增 3 个专用工具**（接入 `AssistantToolbox.dispatch`）：
   - `trace_attachment_lineage(attachment_id, table_name, depth=3)`：围绕目标表或中间临时表，沿 `lineage_dag` 反向（及可选下游）提取所有关联语句块原文并附加前后声明上下文，**只看 10%~15% 核心代码即可理清整条加工链路**。
   - `read_attachment_statement(attachment_id, stmt_id)`：按 `S004` 这类语句编号精确读取完整单条 SQL（含前置注释与真实行号），彻底消除弱模型猜测 `start_line/end_line` 时把一条大 SQL 从中间腰斩的问题。
   - `get_attachment_static_report(attachment_id)`：按分类查看完整静态隐患明细与表读写矩阵（避免首次 Prompt 塞太长时可按需拉取完整报告）。
2. **LLM 友好型语义保真压缩 (`compact_sql_for_llm`)**：
   - 在 `attachments.py` / `sql_static_analyzer.py` 提供严格**保留原始真实行号 (`L120| ...`)** 的视图压缩选项（绝不修改原文件）：
     - 将超过 12 个分支的重复静态映射 `CASE WHEN col = 'A1' ... END` 折叠保留首尾各 3 条并在中间标注 `... [共 48 条同类枚举映射已折叠, L215-L260]`；
     - 将超过 25 项的常量 `IN ('01','02',...)` 列表折叠为代表样本；
     - 剥离纯装饰性横幅注释 (`----====----`)、连续 10 行以上版本变更日志注释与重复空行；
     - 对 `lightweight` 模型可稳定压减 **30%~50% 无效噪声 Tokens**，显著抬高有效注意力密度。

#### 3. 面向轻量/古早模型的 Prompt 减负与纠偏护栏 (`service.py`)
- 当 `model_tier == "lightweight"` 时：
  - **动态工具集裁剪**：纯方言长文本模式下隐藏 `search_catalog`/`inspect_table`/`run_readonly_sql`/`explain_sql`/`propose_write_sql`，仅暴露最相关的 5 个文档与切片工具（`trace_attachment_lineage`、`read_attachment_statement`、`read_attachment_lines`、`search_attachment`、`search_knowledge`），大幅降低轻量模型选错工具或编造参数的概率；
  - **压缩系统指令密度**：采用更直白、结构化的分步指令模板；
  - **循环防打转自动纠偏**：若连续 2 步以高度重叠参数调用同类工具，自动在工具返回值末尾附加定向纠偏提示（例如推荐尚未检查的具体 `stmt_id` 或静态高危项）。

#### 4. 存储层幂等演进 (`modules/ai_assistant/storage_mixin.py`)
- 在 `ai_assistant_attachments` 表通过 `register_table_schema(..., scope="private")` 幂等新增 `analysis_json TEXT NOT NULL DEFAULT '{}'`，上传/创建粘贴附件时同步完成静态解剖与血缘落库。

---

### 支柱三：抗供应商超时韧性层 + 持久化 `/goal`「不达目的不结束」引擎

对照业界主流长任务 Web Agent（OpenAI Responses API Background Mode + `Last-Event-ID` 断点续流 / LangGraph Checkpointer）设计：

#### 1. 供应商超时、慢首字与截断韧性 (`core/platform/llm_hub.py`)
1. **流间静默超时与 TTFT 动态适配**：
   - 将 `_open_stream` 的固定 `120s` 一刀切改造成基于 `ModelProbe.ttft_ms` 的弹性读超时（默认最低 `120s`，慢供应商按探测延迟上调至 `180s~240s`）；流式读取期间只要持续收到 SSE delta 或心跳注释包，socket read timeout 自动按 chunk 刷新，**防止生成到一半因为总耗时超过 120s 被中途掐断**。
2. **瞬态异常指数退避重试 + 自动减载降级**：
   - 将可恢复瞬态错误（`TimeoutError`、`socket.timeout`、`ConnectionResetError`、`IncompleteRead`、HTTP `408`/`429`/`502`/`503`/`504`）从致命异常中分离：
     - 进入自动退避重试：`2s -> 5s -> 12s`（最多 3 次，遵守取消信号），每次重试前通过事件回调发送 `retry_wait`（如 `供应商响应超时，正在第 2/3 次自动重连并启用紧凑压缩视图...`）；
     - 若重试仍超时，自动启动**减载策略**：对当前单阶段上下文开启 `compact_sql_for_llm` 压缩、减半 `max_tokens`/上下文片段体积，并在 `thinking_mode == "auto"` 时允许降为直接输出（减少供应商推理超时耗时）。
3. **截断自动续写 (`finish_reason == "length"`)**：
   - 当检测到流式或非流式返回 `finish_reason == "length"` 时，自动基于已生成尾段无缝触发续写衔接（最多 3 次），保证超长诊断报告或全量改写 SQL 完整交付不腰斩。

#### 2. 持久化后台任务与断点续流基础设施 (`storage_mixin.py` + `router.py`)
彻底解除“前端 SSE 连接断开 = 服务端分析死亡”的耦合：

1. **新增两张 Layer 2 私有运行态表（`scope="private"`，严禁进 `.dataog.db`）**：
   - `ai_assistant_runs`：
     - 字段：`run_id` (PK), `session_id`, `user_message_id`, `run_mode` (`standard` | `goal`), `status` (`queued` | `probing` | `planning` | `running` | `verifying` | `paused_recoverable` | `completed` | `cancelled` | `failed`), `plan_json`（执行计划与阶段 Checklist）, `checkpoint_json`（当前阶段索引、已覆盖语句/行区间、`ledger_json` 结构化阶段结论账本、已完成步数、累积思考时长、退避计数）, `progress_pct`, `current_stage`, `error_message`, `provider_id`, `model`, `thinking_mode`, `last_event_id`, `created_at`, `updated_at`, `finished_at`。
   - `ai_assistant_run_events`：
     - 字段：`event_id` (`INTEGER PRIMARY KEY AUTOINCREMENT`), `run_id`, `event_type`, `payload_json`, `created_at`（支持按 `session` 删除时级联清理）。
2. **新增后台 Run 生命周期 REST + SSE 端点 (`modules/ai_assistant/router.py`)**：
   - `POST /api/modules/ai_assistant/runs`：创建 `run_id`，落盘用户消息并返回 `{ ok, run, session }`，同时投递独立后台工作线程运行（受 `cancel_ev` 控制而非 HTTP 断开控制）。
   - `GET /api/modules/ai_assistant/runs/{run_id}/events?after_event_id=0`：读取 `Last-Event-ID` Header 或 query 参数，**先无损补推所有错过事件再无缝接续实时流**（15s 发送 `: keep-alive`），因此浏览器切页面、刷新或网络抖动均可零丢失重连。
   - `GET /api/modules/ai_assistant/runs/{run_id}`：查询最新 Checkpoint、Checklist 与事件光标。
   - `POST /api/modules/ai_assistant/runs/{run_id}/resume`：针对 `paused_recoverable` / `failed` 任务，直接读取 `checkpoint_json` 与 `ledger_json` 从断点阶段继续执行（绝不从头重跑）。
   - `POST /api/modules/ai_assistant/runs/{run_id}/cancel`：显式停止后台 Goal 运行并保存截至当前已完成阶段与部分文本。
   - `GET /api/modules/ai_assistant/sessions/{session_id}/active-run`：打开或切回历史会话时自动发现并恢复进行中/可续跑任务。
   - **向后兼容**：保留既有 `POST /chat` 与 `POST /chat/stream` 原样可用。

#### 3. 🎯 `/goal`「不达目的不结束」状态机 (`modules/ai_assistant/goal_runner.py`)
借鉴 Claude/Codex 长任务与 Map-Reduce Ledger 模式，避免弱模型一边看新段一边忘旧段、同时避免对话历史越滚越大：

```text
  probing (探查模型真实窗口 & 超时基准)
     │
     ▼
  planning (制定最合适策略 + 拆解阶段清单 + 设定覆盖率/核验门禁)
     │
     ▼
  running (按甜蜜区逐阶段执行：静态报告 + 本阶段语句块/血缘切片 + 压缩只读账本)
     │    └─ 每阶段完成即持久化 checkpoint_json 与结构化 ledger_json (覆盖的stmt_id/发现问题/涉表变更)
     ▼
  verifying (硬验收门禁 — 不达标自动生成补救子任务回到 running!)
     │    ├─ 门禁1：全量/目标任务覆盖率校验（所有语句区间或目标血缘节点是否均已分析）
     │    ├─ 门禁2：HIGH/MEDIUM 静态隐患是否全部被明确判定（确认问题或说明误报）
     │    ├─ 门禁3：多阶段汇总是否存在前置临时表断链或用户问题悬而未决
     │    └─ 门禁4：是否需要可继续安全重试或续写截断
     ▼
  completed (基于 ledger_json + 核心静态图谱合成全景结构化报告，写入正式 assistant_message)
```

- **可控安全熔断**：单次连续推进设定护栏上限（如默认总步数上限 `25` 步、总壁钟 `1800s`），若触及护栏仍未完成则优雅暂停为 **`paused_recoverable`**，保留完整 `checkpoint_json` 与 `ledger_json`，前端展示「已完成 80%，点击继续推进剩余目标」一键接力，决不丢失任何已分析成果。

---

### 支柱四：前端 macOS 桌面级交互升级 (`static/js/`)

严格使用 [dom.js](../static/js/core/dom.js) 节点工厂与 `<dialog>`（零 `innerHTML`、零 `requestAnimationFrame`）：

1. **工具栏新增「🎯 深度目标模式 (`goal`)」与「模型窗口探查」状态胶囊 (`ai_assistant/view.js`)**：
   - 在工具栏新增模式切换：「标准对话」vs「🎯 Goal 模式（防超时·不达目的不结束）」；
   - 在模型选择器旁展示实时探测标签（如 `~64K窗 · 甜蜜区18K · TTFT 1.1s · 可点击刷新探查`）；上传大 SQL 后，`ExecutionPlanner` 选中最优方案并在对话流实时渲染一张优雅的 **「本次智能拆解方案卡」**（说明为何选该策略、预计分几阶段、覆盖哪些核心血缘）。
2. **「目标执行计划与阶段 Checklist」实时进度卡**：
   - 随 SSE 事件 `plan_ready` / `stage_start` / `stage_done` / `retry_wait` / `verify_result` / `checkpoint_saved` 动态更新：
     - 展示各阶段名称、对应源码行区间（如 `阶段 2/4 · 临时表聚合逻辑 (L620-L1280)`）、勾选态与覆盖率百分比条；
     - 发生供应商超时自动退避重连时，在卡片顶部安静提示重连倒计时与自动减载状态，不弹刺眼报错框；
     - 若任务暂停 (`paused_recoverable`) 或异常中断，直接提供 **「从断点继续执行」** 按钮调用 `/runs/{run_id}/resume`。
3. **无损断线重连 SSE 客户端 (`static/js/core/http.js` + `ai_assistant/api.js`)**：
   - 在 `http.js` 新增 `streamSseGet(url, { signal, lastEventId, onEvent })` 支持 `Last-Event-ID`；
   - `AiAssistantView` 切换到进行中的会话或重新打开窗口时，自动调用 `active-run` 恢复实时观察与控制。
4. **系统配置 → AI 模型增强 (`static/js/views/settings_view.js`)**：
   - 新增「探查能力与窗口」按钮触发 `/api/platform/llm/probe-model`，回显测得的上下文窗口、模型分级 (`lightweight` / `standard` / `flagship`)、真实 Token 比率与首字延迟。

---

## 三、分阶段实施路线图（共 6 个阶段）

| 阶段 | 核心交付 | 重点改动文件 |
| :--- | :--- | :--- |
| **Phase 1** | **静态 SQL 解剖 + 血缘 DAG + 语义压缩 + 手术刀工具** | `modules/ai_assistant/sql_static_analyzer.py`（新增）、`attachments.py`、`tools.py`、`storage_mixin.py`、`tests/test_ai_assistant_and_kb.py` |
| **Phase 2** | **模型动态能力探查器 + 分级预算 + 抗超时退避与续写** | `core/platform/llm_hub.py`、`core/platform/routes/appearance_and_llm.py`、`core/platform/schemas.py`、`tests/test_platform.py` |
| **Phase 3** | **ExecutionPlanner 自适应规划 + 轻量模型 Prompt 减负** | `modules/ai_assistant/service.py`、`modules/ai_assistant/schemas.py` |
| **Phase 4** | **持久化 Run/Event 表 + Goal 状态机 + 断点续流 API** | `modules/ai_assistant/goal_runner.py`（新增）、`storage_mixin.py`、`router.py`、`tests/test_ai_assistant_and_kb.py` |
| **Phase 5** | **前端 SSE 重连客户端 + Goal Checklist 卡片 + 探查 UI** | `static/js/core/http.js`、`static/js/core/platform_api.js`、`static/js/apps/ai_assistant/{api.js,view.js,style.css}`、`static/js/views/settings_view.js` |
| **Phase 6** | **全链路端到端测试（Node Scene 7）、文档同步与发版闭环** | `tests/test_platform.py`、`tests/test_ai_assistant_and_kb.py`、版本与文档同步、GitHub Flow 打标发版 |

---

## 四、关键工程约束与避坑指南

1. **TestClient + SSE 死锁禁区**：[tests/test_modules_and_api.py](../tests/test_modules_and_api.py) 的自定义异步 ASGI `TestClient` 会在消费完请求体后阻塞等待 `response_complete`，**任何 `StreamingResponse` 生成器内绝不可调用 `await request.is_disconnected()`**；断连清理依赖 `asyncio.Queue` + `finally` 处理，且后台 `goal_runner` 本身与客户端断连解耦（仅响应显式 `/cancel` 接口）。
2. **Python 3.14 `ResourceWarning` 与并发句柄**：所有 `urllib.error.HTTPError` 必须显式 `exc.close()`；Run 事件写入 SQLite 使用短生命周期 `ctx.store.module_db(scope="private")`，防止多线程与 HTTP 线程锁竞争。
3. **三层存储与 Gate 2 鉴权铁律**：
   - 探针缓存存入私有库 `llm_providers.extra_json`，`ai_assistant_runs`、`ai_assistant_run_events` 与 `analysis_json` 必须严格声明 `scope="private"`，导出 `.dataog.db` 时绝不可包含；
   - 新增每个平台/模块 FastAPI 路由均须注入 `ctx: PlatformContext = Depends(get_platform_context)` 并带完整可由 `typing.get_type_hints(..., include_extras=True)` 解析的类型注解，异常信息经 `redact_private_storage_paths()` 脱敏；
   - **严禁改动 `core/platform/workspace_hub.py` 中的 `WORKSPACE_FORMAT_VERSION = "2.7.0"`**。
4. **前端静默安全扫描红线**：`static/js/**` 零 `innerHTML` / `outerHTML` / `insertAdjacentHTML` / `document.write`，零 `alert`/`confirm`/`prompt`，流式节流一律使用 `setTimeout`（禁止使用 `MockNode` 中不存在的 `requestAnimationFrame`）。
5. **发版版本号规划**：本轮改造涉及较大新特性（`/goal` 状态机、SQL 静态血缘分析与模型探查），建议按 SemVer 升为 **`v3.4.0`**（若需继续复用 `3.3.0+` 同二级版本增量更新包通道，也可由需求方在实施前确认选用 **`v3.3.2`**），按 [release-and-ship Skill](../.agents/skills/release-and-ship/SKILL.md) 同步 6 处版本号并完成本地双门禁 (`unittest` + `--smoke-test`) 后再合入 `main` 与推送 Tag。

---

## 五、测试验收矩阵清单

- **`tests/test_platform.py`**：
  - `LLMHub.fetch_models()` 解析 `/models` 各类扩展上下文字段；
  - `LLMHub.probe_model_capabilities()` 实测 TTFT/token_ratio 校准、Cache TTL 命中与失效降级；
  - `lightweight` 定档（含 `deepseek-v4-flash`）正确收缩 `effective_reasoning_budget` 与 `recommended_chunk_tokens`；
  - 400 超限正则反哺校准探针缓存；
  - HTTP `503`/读超时指数退避重试成功、`finish_reason=="length"` 自动续写拼接。
- **`tests/test_ai_assistant_and_kb.py`**：
  - `sql_static_analyzer.py` 解析 3,000+ 行含临时表/游标循环/动态 SQL/无 `WHERE` 删除的存储过程，验证血缘 DAG、精确行号、`static_findings` 与 `compact_sql_for_llm` 压缩比率及行号保真；
  - `trace_attachment_lineage` 与 `read_attachment_statement` 切片工具精准返回关联 SQL；
  - `ExecutionPlanner` 在 `deepseek-v4-flash` 上自动避开硬塞 80K 盲区，制定 `lineage_sliced` / `goal_driven_staged` 策略；
  - Goal 引擎闭环测试：模拟前两阶段与偶发超时重试、验收门禁发现遗漏高危项后自动追加第 3 轮补救子任务直至达标完成、`paused_recoverable` + `/runs/{run_id}/resume` 从 Checkpoint 断点续跑、`/runs/{run_id}/events?after_event_id=...` 断点重放零丢帧；
  - 工作区导出 `.dataog.db` 零泄露及删除会话级联清理 `runs` / `run_events`；
  - Node.js E2E（Scene 7）：模型探查标签、策略计划卡、Goal Checklist 实时勾选、退避重连提示、断点继续按钮全交互验证。
