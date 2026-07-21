# Immortal Memory v1.1 Living Self 设计规格

状态：已批准，可进入实施计划
方案：C，动态事实源＋结构化 Living Self＋按任务编译 Context Pack  
目标版本：`1.1.0`  
基线版本：`1.0.0`，commit `19c33251c39bdaa99ae19c4fed709b2071fef7ac`  
设计确认：用户于 2026-07-19 明确回复「按你的推荐方案 c 来」
审批授权：用户随后明确授权主代理自行审批并组织专家团复核；主代理依据专家审查修订本规格后批准进入实施

## 1. 目的

Immortal Memory v1.0.0 已经具备真实采集、恢复、检索、画像、任务上下文、运行控制和隐私扫描能力，但当前产品界面的核心仍是运行状态，无法清楚回答：

1. 系统今天真正记住了什么？
2. 它对用户的理解发生了什么变化？
3. 哪些理解来自本人证据，哪些只是系统推断？
4. 哪些历史判断产生了实际结果？
5. 本次 Agent 调用了哪些记忆，结果是否有效？

v1.1 的目标不是添加更多静态看板卡片，而是建立一条真实、可验证的个人认知闭环：

```text
真实记录
  -> 归因与作用域
  -> 结构化主张
  -> Living Self
  -> Judgment Memory
  -> 任务级 Context Pack
  -> 使用结果
  -> 纠正与版本演化
```

## 2. 产品定位

Immortal Memory 是一个有证据、会成长、可纠正、能在真实任务中调用并验证结果的个人认知系统。

产品承诺：

> 记住事实，理解变化，复用判断，守住边界。

系统不是用户本人，也不得声称替代用户。系统提供的是：

- 有来源的事实回忆；
- 有作用域的用户模型；
- 可解释的历史判断；
- 经过隐私过滤的任务上下文；
- 对推断、未知和冲突的诚实标记；
- 用户可确认、纠正和回滚的派生理解。

## 3. 设计来源与取舍

### 3.1 吸收 Yourself

- Self Memory 与 Persona 的双层认知；
- 生成前预览；
- 用户纠正；
- 模型版本与回滚；
- 用户对数字自我的明确控制权。

不吸收：

- 由自由文本组成的单一大型人格文件；
- 默认角色扮演；
- 弱归因、弱证据的自动画像；
- 把全部上下文永久注入 Agent。

### 3.2 吸收女娲

- 心智模型；
- 决策启发式；
- 表达 DNA；
- 反模式；
- 内在张力；
- 诚实边界；
- 跨域复现、生成力、区分度三重验证；
- 已知立场、边缘问题和表达辨识度的分离评测。

不吸收：

- 默认第一人称冒充人物；
- 高成本全量蒸馏进入同步请求；
- 用静态字段检查或 LLM 自评替代真实证据；
- 面向公众人物的来源规则直接套用到私人第一方语料；
- 将人物认知固定在单一 `SKILL.md`。

### 3.3 保留 Immortal

- `~/.immortal/` 是私有真相源；
- 原始记录只追加；
- `daily/*.jsonl[.gz]` 与 `index.jsonl` 是事实层；
- SQLite、画像、摘要、Obsidian、GetNote、Living Self 均为可重建派生层；
- Agent 默认不读取原始仓；
- `agent-context` 是跨 Agent 的稳定入口；
- 本地优先、loopback 服务、账户保护、恢复校验和公开发布隐私扫描。

## 4. 范围

### 4.1 v1.1 必须完成

1. 新增可信归因与作用域数据契约。
2. 新增结构化 Living Self 模型及版本机制。
3. 新增 Judgment Memory 真实存储、查询和纠正流程。
4. 升级 Context Pack，使其区分事实、归纳、推断、建议和未知。
5. 记录上下文使用与结果反馈。
6. 建立面向用户价值的七模块真实看板。
7. 将现有运行、来源、备份和诊断下沉到「系统」模块。
8. 修复公开包缺少 `cards.py`、但 CLI 仍引用该文件的发布漂移。
9. 保持 v1.0 CLI、原始 vault 和备份可兼容。
10. 完成全量测试、真实安装部署、脱敏扫描、版本、tag、release 和 GitHub 更新。

### 4.2 v1.1 不做

- 云端多用户服务；
- 多人共享同一 vault；
- 自动替用户执行不可逆决定；
- 人脸、声音或数字人视频；
- 对所有原始内容进行向量重建；
- 强制引入图数据库；
- 训练或微调专用模型；
- 默认把用户表达风格应用于所有 Agent；
- 删除或改写历史原始记录。

## 5. 核心不变量

以下约束高于界面和实现便利：

1. 原始证据不可被画像纠正覆盖或删除。
2. 他人陈述不得直接进入用户自我模型。
3. 每条已确认主张必须有证据，或明确标记为用户手工声明。
4. 推断不得展示为事实。
5. 作用域外的主张不得进入 Context Pack。
6. 表达风格不得提高事实或判断置信度。
7. 纠正必须产生审计事件，不做静默覆盖。
8. 所有公开导出默认排除私有正文。
9. 看板不得用静态假数据或仅前端状态模拟成功。
10. HTTP 200、测试绿或字段齐全都不能单独证明生产可用。
11. `index.jsonl` 与 SQLite 的 offset、size 相等不能证明索引完整，必须验证源前缀指纹和记录 ID 对账。
12. SQLite 不可用或完整性未知时，产品 API 不得回退为全量扫描 1GB 级 JSONL。
13. 任何注册到 CLI 或每日流水线的命令都必须存在于干净安装包，必需阶段失败必须返回非零退出码。
14. 没有异盘、全量哈希和隔离恢复证据时，不得执行生产迁移。
15. 日常 Notes 同步不得扫描完整 `index.jsonl` 或全部 `daily/`；跨文件恢复必须由持久事务元数据驱动。

### 5.1 已确认的 v1.0 生产 P0

2026-07-19 只读核查发现：

- `index.jsonl` 为 471,912 行、1,056,490,555 字节；
- `search_index.db` 的 `docs` 为 470,712 行；
- SQLite `meta.last_offset` 与 `meta.last_size` 均等于 JSONL 当前字节数；
- SQLite 比 JSONL 少 1,200 条，说明现有「offset 已追平」信号存在假绿。

v1.1 编码前必须先增加索引完整性测试与 staging 全量重建能力。生产切换前必须完成 JSONL ID 与 staging SQLite ID 的双向对账，并用原子替换切换数据库。

公开 v1.0 仓库还注册了 `cards.py`、`project.py`、`obsidian_notes_sync.py` 等缺失脚本，而 live 安装存在同名文件。v1.1 必须逐项决定公开安全实现或移除入口，禁止直接复制可能含私有逻辑的 live 文件。

### 5.2 Notes 事务边界

Obsidian 手工笔记仍写入 L0 的 `daily/*.jsonl` 与 `index.jsonl`，但日常同步不得把扫描全量事实层当作事务恢复机制。新增两类仅用于恢复的私有元数据：

- `notes/manifest.json`：记录源文件指纹、最近成功事务、迁移状态和 bounded 统计；
- `notes/transactions/<tx_id>.json`：记录待写事实的稳定 ID、严格校验后的 daily 相对路径、daily/index 预写偏移、序列化行长度、SHA256、事务阶段和时间。

事务必须在统一 source lock 内执行：

1. 读取目标文件当前长度，生成完整待写字节；
2. 原子写入并 fsync `prepared` journal，同时 fsync journal 父目录；
3. 在 journal 声明的精确偏移写 daily，fsync 后把阶段更新为 `daily_committed`；
4. 在 journal 声明的精确偏移写 index，fsync 后把阶段更新为 `index_committed`；
5. 原子更新 manifest，再把 journal 标记为完成或删除。

恢复只读取未完成 journal，并检查声明偏移处的精确字节：

- 字节完全相同则视为该侧已提交；
- 文件长度等于预写偏移则补写；
- 长度、哈希、目标路径或偏移不一致则 fail closed，不猜测、不覆盖。

这使崩溃恢复成本与未完成事务数量相关，而不是与 1GB 级 index 或数千个 daily 文件相关。

其他硬约束：

- 目录遍历与文件读取必须由根目录 fd 锚定，每级使用 `dir_fd` 与 `O_NOFOLLOW`；
- 总读取预算按实际读取字节计数，文件并发增长也不得突破剩余预算；
- daily 日期必须经 `date.fromisoformat()` 严格解析，目标 resolved parent 必须等于 vault 的 `daily/`；
- append helper 必须返回已写字节与 fsync 状态，`facts_committed` 只依据真实结果；
- 错误状态必须原子覆盖旧成功，并保留 `last_success`、事务 ID、失败阶段和待修复方向；
- capability 必须绑定到具体 subparser 的真实 handler 对象，命令名、注释、同名伪函数或无 handler 入口均不得解锁。

历史重复、孤儿和旧版无 journal 写入由显式一次性 `notes-migrate` 处理。迁移使用磁盘上的临时 SQLite catalog 与 checkpoint，流式扫描受最大文件数、最大字节数和最大时长约束；冲突 fail closed；去重后的 daily/index 通过 staging 文件、校验和可恢复发布 journal 切换。

新 vault 在初始化时创建 `migration_status=not_required` 的空 manifest。只要 vault 已有非空事实层但缺少兼容 manifest，或存在旧版 `notes/state.json`，`notes-sync` 就返回 `notes_migration_required`；它不得为了判断是否存在旧 Notes 记录而扫描全库。安装升级与生产切换必须显式运行并验证 `notes-migrate`，完成后写入版本化 migration marker。日常 `notes-sync` 不偷偷触发全库迁移。

## 6. 数据分层

### 6.1 L0：Raw Evidence

继续使用现有 `daily/`、`index.jsonl`、SQLite 索引和 source metadata。

L0 只负责：

- 原始内容；
- 来源；
- 采集时间；
- 原作者；
- 账户；
- 内容哈希；
- 导入批次；
- 删除标记或源端失效状态。

任何新模型均通过 `evidence_id` 引用 L0，不复制无边界的完整私密正文。

SQLite 是可重建读模型，不是证据权威源。`evidence_id` 必须来自 JSONL 事实记录的稳定 ID，不能使用 SQLite rowid。

新增 `EvidenceRef`：

```json
{
  "evidence_id": "事实层稳定 ID",
  "source": "codex|claude|feishu|local|web|custom",
  "raw_id": "源端或 raw 层 ID，可为空",
  "content_hash": "sha256:...",
  "status": "available|source_broken|source_deleted",
  "observed_at": "...",
  "privacy": "private|restricted|context_safe|public"
}
```

旧记录缺少稳定 ID 时，使用可重复的来源、时间和内容哈希生成迁移 ID，并保留 `source_broken` 或 `source_deleted`，不能伪装成可直接验证的证据。

### 6.1.1 统一事件 envelope

所有 v1.1 权威事件使用同一 envelope：

```json
{
  "event_id": "evt_...",
  "event_type": "claim.created",
  "stream_id": "clm_...",
  "stream_version": 1,
  "schema_version": 1,
  "request_id": "req_...",
  "idempotency_key": "idem_...",
  "actor": {
    "kind": "owner|system|migration",
    "id": "owner|service|migration-name"
  },
  "occurred_at": "...",
  "expected_version": 0,
  "payload": {},
  "previous_status": null,
  "migration_source": null
}
```

事件要求：

- 相同 idempotency key 与相同 payload 返回第一次结果；
- 相同 idempotency key 与不同 payload 返回 `idempotency_conflict`；
- `expected_version` 不匹配返回 `version_conflict`；
- event append 是权威提交，current view 可以在崩溃后通过 replay 修复；
- 尾部部分写入必须显式报错并进入恢复流程，不静默跳过；
- 每个 current view 保存 `based_on_event_seq` 和 `stream_version`。

### 6.2 L1：Attribution Claims

新增派生目录：

```text
~/.immortal/model/
  claims/
    events.jsonl
    current.jsonl
  attribution/
    latest-report.json
```

`events.jsonl` 是只追加事件流。`current.jsonl` 是可重建当前视图。

主张对象 `Claim`：

```json
{
  "schema_version": 1,
  "revision": 1,
  "claim_id": "clm_...",
  "subject": {
    "kind": "owner",
    "id": "owner"
  },
  "speaker": {
    "kind": "owner|other|system|unknown",
    "id": "..."
  },
  "claim_type": "fact|preference|value|commitment|decision|lesson|relationship|style|emotion|request",
  "statement": "结构化、已脱敏的主张正文",
  "evidence_ids": ["ev_..."],
  "counter_evidence_ids": [],
  "source_kind": "direct|quoted|observed|inferred|user_declared",
  "confidence": 0.0,
  "confidence_basis": {
    "speaker": 0.0,
    "recurrence": 0.0,
    "source_quality": 0.0,
    "explanation": "..."
  },
  "role_scope": ["personal|work|creator|family|custom"],
  "domain_scope": ["general|business|content|technical|relationship|project|risk|custom"],
  "custom_scope_ids": [],
  "privacy": "private|restricted|context_safe|public",
  "valid_from": null,
  "valid_to": null,
  "status": "candidate|confirmed|rejected|superseded",
  "created_at": "...",
  "updated_at": "...",
  "based_on_event_seq": 1
}
```

约束：

- `speaker.kind != owner` 时，除非内容明确是他人对用户的外部评价，否则不得生成 `subject.kind = owner` 的直接事实。
- `source_kind = inferred` 的主张不能自动进入 `confirmed`。
- `confidence` 不是 UI 百分比装饰，必须由可检查规则计算并保留计算原因。
- `schema_version` 与对象 `revision` 分离，禁止使用 `model_version` 同时表达两者。
- `custom` scope 必须带稳定的 `custom_scope_ids`，不能让所有自定义作用域互相匹配。
- `privacy = private` 的正文不得进入 Context Pack。
- 旧 `reviewed/profile_memories.jsonl` 只读迁移为 `Claim candidate`，原文件不删除。
- 旧 `profile_nuwa.accepted` 只能作为候选差异提示，不能迁为 confirmed。
- 代码内 pinned item、fallback 文案和静态 reference 不能迁为 `user_declared`。

### 6.3 L2：Living Self

新增：

```text
~/.immortal/model/living-self/
  current.json
  current.md
  versions/
    <version-id>.json
    <version-id>.md
  evaluations/
    <version-id>.json
```

Living Self 包含八个模块：

1. `identity_commitments`
2. `values`
3. `expression_dna`
4. `mental_models`
5. `decision_heuristics`
6. `anti_patterns`
7. `tensions`
8. `honest_boundaries`

模型项 `SelfModelItem`：

```json
{
  "schema_version": 1,
  "revision": 1,
  "item_id": "self_...",
  "kind": "mental_model",
  "title": "先形成独立预判，再使用 AI 交叉验证",
  "summary": "...",
  "evidence_ids": ["ev_..."],
  "claim_ids": ["clm_..."],
  "counter_evidence_ids": [],
  "confidence": 0.84,
  "validation": {
    "cross_domain_recurrence": 3,
    "generative_power": "tested|untested|failed",
    "distinctiveness": "high|medium|low"
  },
  "application": ["..."],
  "failure_conditions": ["..."],
  "role_scope": ["work"],
  "domain_scope": ["business", "technical"],
  "valid_from": "...",
  "valid_to": null,
  "status": "candidate|confirmed|rejected|superseded",
  "last_reviewed_at": "...",
  "based_on_claim_seq": 47
}
```

Living Self 版本容器：

```json
{
  "version_id": "lsv_...",
  "parent_version_id": "lsv_...|null",
  "status": "candidate|confirmed|superseded",
  "generation_reason": "migration|claim_change|manual_restore|scheduled_rebuild",
  "content_hash": "sha256:...",
  "based_on_claim_seq": 47,
  "generated_at": "...",
  "confirmed_at": null,
  "sections": {}
}
```

Living Self 生成规则：

- 一次性情绪和单点表达不能升级为长期模型。
- 心智模型至少满足两个不同时间点和两个不同语境的证据，或由用户手工确认。
- 内在冲突不得通过择一删除解决，必须表达为 `tension`。
- 过期项目事实不进入长期身份，但可保留在时间线。
- `expression_dna` 是独立适配器，默认不进入判断模型。
- 每次生成产生不可变版本，`current` 只是指向最新确认版本的派生视图。
- SelfModelItem 的 confirm、reject 和 correct 通过 Claim correction 与新 Living Self 版本完成，不直接原地修改版本内容。

### 6.4 L3：Judgment Memory

新增：

```text
~/.immortal/judgment/
  events.jsonl
  current.jsonl
  evaluations.jsonl
```

判断卡 `JudgmentCard`：

```json
{
  "card_id": "jdg_...",
  "title": "...",
  "situation": "...",
  "goal": "...",
  "constraints": ["..."],
  "signals": ["..."],
  "decision": "...",
  "alternatives": ["..."],
  "outcome": {
    "status": "unknown|positive|mixed|negative",
    "summary": "...",
    "observed_at": null
  },
  "lesson": "...",
  "next_trigger": "...",
  "evidence_ids": ["ev_..."],
  "claim_ids": ["clm_..."],
  "privacy": "private|restricted|context_safe|public",
  "status": "candidate|confirmed|rejected|retired",
  "created_at": "...",
  "updated_at": "..."
}
```

判断卡可以由历史记录产生候选，但只有以下情况才能进入 Context Pack：

- 用户确认；
- 有明确决定和后续结果证据；
- v1.1 首版默认关闭自动确认。未来启用时必须另行定义阈值和高风险分类。

v1.0 缺少的 `cards.py` 不直接补一个孤立脚本。v1.1 将其能力重建为 `judgment_store.py`、`judgment_service.py` 和受测试的 CLI/API，并保留旧 `cards` 命令兼容层。

### 6.5 L4：Context Pack

现有 `agent-context` 保持为稳定入口，但内部升级为基于结构化数据的编译器。

持久化目录：

```text
~/.immortal/contexts/
  events.jsonl
  current.jsonl
  previews/
    <preview-id>.json
  packs/
    <context-id>/
      context.json
      TASK_CONTEXT.md
```

`events.jsonl` 是上下文生命周期的权威源，`current.jsonl` 是可重建视图。预览会落盘到受 TTL 管理的私有 preview 区，但不会成为 Agent 可读取的正式 Context Pack。

上下文包必须包含：

```json
{
  "context_id": "ctx_...",
  "task": "...",
  "mode": "advisor|writer|reviewer|business|project|custom",
  "lifecycle_status": "preview|compiled|consumed|outcome_recorded",
  "availability_status": "active|expired",
  "budget": {
    "max_chars": 24000,
    "used_chars": 0
  },
  "sections": {
    "verified_facts": [],
    "confirmed_self_models": [],
    "judgment_cards": [],
    "counter_evidence": [],
    "inferences": [],
    "unknowns": []
  },
  "provenance": {
    "evidence_ids": [],
    "claim_ids": [],
    "self_model_item_ids": [],
    "judgment_card_ids": []
  },
  "privacy_policy": {
    "excluded_count": 0,
    "reasons": []
  },
  "source_revision": {
    "claims_event_seq": 0,
    "living_self_version": "lsv_...",
    "judgments_event_seq": 0,
    "compiler_version": "1.1.0",
    "policy_version": 1
  },
  "preview_hash": "...",
  "content_hash": "sha256:...",
  "generated_at": "...",
  "expires_at": "..."
}
```

编译顺序：

1. 解析任务、模式、角色和领域。
2. 运行现有 preflight。
3. 检索事实和候选主张。
4. 只选择作用域相符的确认模型。
5. 选择相关判断卡和反例。
6. 排除私密正文、凭证、无关人物和越权来源。
7. 明确区分事实、归纳、推断和未知。
8. 生成用户预览，持久化 `preview_id`、source revision、hash 和 TTL。
9. 编译请求只提交 `preview_id`、`preview_hash` 和用户主动移除的 item IDs。
10. 服务重新读取预览，验证 revision、hash、TTL 和移除 ID 子集后生成正式包。
11. 记录本次实际使用的模型与证据。

复制到剪贴板不等于 `consumed`。只有用户明确点击「已交给 Agent」，或 Agent Bridge 返回成功使用回执后，才能进入 consumed。

默认预算：

- 用户可读 Markdown：最多 24,000 字符；
- 结构化 JSON：不复制原始私密正文；
- 每类最多 20 项；
- 单条摘要最多 500 字符；
- 用户可显式提高预算，但不能绕过隐私策略。

### 6.6 L5：Outcome Feedback

新增：

```text
~/.immortal/outcomes/
  events.jsonl
```

`OutcomeEvent`：

```json
{
  "outcome_id": "out_...",
  "context_id": "ctx_...",
  "adopted": "yes|partial|no|unknown",
  "result": "positive|mixed|negative|unknown",
  "summary": "...",
  "confirmed_refs": [
    {"kind": "claim|self_model|judgment", "id": "...", "revision": 1}
  ],
  "challenged_refs": [],
  "created_at": "..."
}
```

结果事件不得自动删除或覆盖模型。它只会：

- 提高或降低候选模型的支持度；
- 生成待确认修正；
- 建议产生新版本；
- 将失败案例加入反例。

跨事件流更新不假装成单事务。每个派生视图保存 watermark。Claim correction 已提交但 Living Self 重建失败时，Living Self 标记 `stale`，信任页显示落后 event seq，旧 current 可继续只读，但不得声称已经吸收新纠正。

## 7. 状态机

### 7.1 Claim

```text
candidate -> confirmed
candidate -> rejected
confirmed -> superseded
rejected -> candidate
```

`rejected -> candidate` 只能由新证据或用户明确重新考虑触发。

修正已确认主张时：

1. 原主张进入 `superseded`；
2. 创建新主张；
3. 写入 correction event；
4. 重新生成受影响的 Living Self 版本。

### 7.2 Judgment Card

```text
candidate -> confirmed -> retired
candidate -> rejected
confirmed -> candidate
```

已有结果被新证据推翻时，卡片退回候选状态，不删除历史版本。

### 7.3 Context Pack

Context 使用两个正交状态：

```text
lifecycle: preview -> compiled -> consumed -> outcome_recorded
availability: active -> expired
```

只有 `compiled + active` 后才能首次提供给 Agent。`preview + expired` 不能编译，`compiled + expired` 不能再次分发。已经 consumed 的 Context 即使随后 expired，仍允许补录 outcome。用户不能直接修改预览正文，只能提交待移除 item IDs；服务基于原始预览重新生成正式包。

## 8. 服务边界

新增或拆分以下模块：

| 模块 | 责任 | 不负责 |
|---|---|---|
| `model_types.py` | 数据类型、枚举、校验 | 文件读写、HTTP |
| `event_store.py` | 只追加事件、原子写、重放 | 业务状态转换 |
| `evidence_catalog.py` | L0 ID 解析、证据存在性、断链状态 | 业务模型 |
| `index_integrity.py` | staging reindex、ID 对账、原子切库 | 检索排序 |
| `claim_store.py` | Claim 持久化与当前视图 | 自动归因 |
| `model_migration.py` | v1.0 到 v1.1 确定性转换、checkpoint、dry-run、报告 | 在线业务写入 |
| `attribution_service.py` | 说话人、对象、作用域、隐私和 confidence basis | UI |
| `living_self_service.py` | 模型生成、版本、差异和回滚 | 原始采集 |
| `judgment_store.py` | 判断卡存储与状态 | Context 排序 |
| `context_store.py` | preview、Context 事件、过期、幂等、current view | 检索排序 |
| `context_compiler.py` | 检索、过滤、预算、编译 | 生命周期持久化、Agent 执行 |
| `outcome_store.py` | 使用结果事件 | 自动修改确认模型 |
| `product_data.py` | 面向产品的只读聚合 | 运维命令 |
| `product_http.py` | `/api/v2` 路由、安全和错误协议 | HTML |
| `product_ui.py` | 七模块界面 | 直接读 vault 文件 |

前端静态资产使用原生 ES modules：

```text
core/product_assets/
  product.css
  api.js
  app.js
  router.js
  dialog.js
  views/
    home.js
    memories.js
    self.js
    judgments.js
    contexts.js
    trust.js
    system.js
```

不引入 Node 构建。`product_ui.py` 只输出语义化 HTML 壳和本地资产引用。最终 CSP 移除 `unsafe-inline`。

现有超大文件逐步瘦身：

- `profile_review.py` 保留旧入口，业务迁移到上述服务；
- `immortal.py` 只保留 CLI 注册与薄调用；
- `dashboard.py` 保留旧看板兼容，不承载新产品逻辑；
- `control_center_ui.py` 迁移为壳层，最终消费 `/api/v2`；
- `control_data.py` 只负责系统运维读模型。

任何新模块都必须可以在不启动 HTTP 和不读取真实 vault 的情况下独立测试。

## 9. HTTP API v2

所有 API 只绑定 loopback，沿用 Host 校验和安全响应头。写操作要求：

- `Content-Type: application/json`
- 同源请求
- `X-Immortal-Request-Id`
- `Idempotency-Key`
- `If-Match` 或请求体 `expected_version`
- 明确动作
- 审计事件
- 请求体硬上限
- 精确 scheme、host、port 同源

### 9.1 首页

`GET /api/v2/home`

返回：

- 今日新增记忆；
- 最近理解变化；
- 待确认数；
- 最近上下文调用；
- 最近结果；
- 产品健康摘要；
- 影响用户价值的异常。

不得返回私密正文。

### 9.2 记忆

- `GET /api/v2/memories`
- `GET /api/v2/memories/{id}`

支持：

- `q`
- `source`
- `person`
- `project`
- `topic`
- `from`
- `to`
- `limit`
- `cursor`

列表只返回摘要。详情只返回请求的单条记录，并执行脱敏。

分页使用 opaque keyset cursor，禁止 offset 深分页。SQLite 完整性未知时返回 `index_unavailable`，不扫描完整 JSONL。

### 9.3 我

- `GET /api/v2/self`
- `GET /api/v2/self/items/{id}`
- `GET /api/v2/self/versions`
- `GET /api/v2/self/versions/{id}/diff`
- `POST /api/v2/self/items/{id}/actions`
- `POST /api/v2/self/versions/{id}/restore`

动作：

- `confirm`
- `reject`
- `correct`
- `reconsider`

`correct` 必须包含新正文和原因。

### 9.4 判断

- `GET /api/v2/judgments`
- `GET /api/v2/judgments/{id}`
- `POST /api/v2/judgments/{id}/actions`
- `POST /api/v2/judgments`

支持候选确认、纠正、结果补录和退役。

### 9.5 使用

- `POST /api/v2/contexts/preview`
- `POST /api/v2/contexts`
- `GET /api/v2/contexts`
- `GET /api/v2/contexts/{id}`
- `POST /api/v2/contexts/{id}/consume`
- `POST /api/v2/contexts/{id}/outcomes`

预览不写入正式 Context Pack。正式编译返回可读 Markdown、结构化清单和排除说明。

预览响应必须包含 `preview_id`、`source_revision`、`preview_hash`、`expires_at`、入选项和排除原因。

### 9.6 信任

`GET /api/v2/trust`

返回：

- 说话人未知；
- 他人观点污染候选；
- 无证据主张；
- 低置信度；
- 过期模型；
- 冲突；
- 来源断裂；
- 隐私排除；
- 最近纠正；
- 模型评测。

### 9.7 系统

- `GET /api/v2/system`
- 现有安全动作继续通过受控 job API。

返回采集、索引、备份、服务、版本、诊断和最近运行。不得与产品价值指标混成同一健康分数。

### 9.8 错误协议

沿用：

```json
{
  "error": {
    "code": "stable_machine_code",
    "message": "用户可读错误",
    "detail": "安全、脱敏后的诊断",
    "retryable": false
  }
}
```

新增标准错误：

- `invalid_transition`
- `evidence_not_found`
- `scope_mismatch`
- `private_content_blocked`
- `context_budget_exceeded`
- `stale_preview`
- `idempotency_conflict`
- `migration_required`
- `index_unavailable`
- `version_conflict`
- `request_too_large`

## 10. 看板信息架构

### 10.1 一级导航

```text
首页　记忆　我　判断　使用　信任　系统
```

### 10.2 首页

首页必须先展示价值，再展示运行状态：

1. 「今天记住了什么」
2. 「对你的理解发生了什么变化」
3. 「需要你确认」
4. 「最近一次记忆调用」
5. 「调用结果」
6. 「影响使用的健康异常」

健康正常时只显示一条紧凑状态，不占据首页主体。

### 10.3 记忆

- 时间、人物、项目、主题四种入口；
- 真实筛选和分页；
- 单条详情证据抽屉；
- 显示说话人和谈论对象；
- 显示是否参与 Living Self；
- 可从记忆发起纠错，但不能修改原文。

### 10.4 我

八模块视图：

- 长期身份与责任；
- 价值排序；
- 表达 DNA；
- 心智模型；
- 决策启发式；
- 反模式；
- 内在张力；
- 诚实边界。

每项显示：

- 置信度；
- 作用域；
- 支持证据数；
- 反例数；
- 状态；
- 版本变化；
- 查看证据；
- 确认、纠正、拒绝。

### 10.5 判断

- 候选、已确认、已有结果、失败案例；
- 情境、选择、结果、教训；
- 相似历史判断；
- 结果补录；
- 一键用于当前任务。

### 10.6 使用

这是核心行动页：

1. 输入当前任务；
2. 选择模式；
3. 预览系统准备使用的事实、模型、判断卡和排除项；
4. 用户可移除不相关内容；
5. 编译 Context Pack；
6. 复制、下载或交给 Agent；
7. 返回后记录结果。

按钮必须有真实状态：

- `准备中`
- `预览完成`
- `编译中`
- `可使用`
- `已交给 Agent`
- `待记录结果`
- `结果已记录`
- `失败`

### 10.7 信任

信任页不是日志页。它回答：

- 系统在哪些地方可能理解错了；
- 为什么它认为这条内容属于用户；
- 哪些结论缺乏证据；
- 哪些内容因隐私被排除；
- 哪些模型已过期；
- 用户最近纠正了什么。

### 10.8 系统

保留并整合 v1.0 Control Center：

- 运行；
- 来源；
- 备份；
- 诊断；
- 服务；
- 版本；
- 日志；
- 受控维护动作。

旧八模块入口在一个版本周期内保留兼容跳转。

## 11. 视觉和交互原则

产品调性：

- 深色、克制、精密；
- 像个人认知仪表，而不是服务器监控大屏；
- 信息层级清楚，避免满屏状态卡；
- 动效只用于状态变化和空间关系，不用装饰性循环动画；
- 按钮有明确材质层级、按压状态、焦点状态和禁用原因；
- 桌面、移动和键盘操作均可用；
- 支持 `prefers-reduced-motion`；
- 关键状态不能只依赖颜色。

默认视觉结构：

- 左侧窄导航；
- 顶部任务和全局状态；
- 中央主内容；
- 右侧证据或详情抽屉；
- 宽表格仅用于记忆与审计，其他页面优先使用结构化卡片和时间线。

交互约束：

- 七个页面支持深链接、刷新、前进和后退；
- 路由切换取消旧请求或使用 generation token，旧响应不得覆盖新页面；
- modal 打开时焦点进入、背景 inert、Escape 关闭、焦点归还；
- 所有输入有显式 label，触控目标至少 44px，移动输入字体至少 16px；
- 导航使用 `aria-current`；
- 响应设置 `Cache-Control: no-store`；
- 全局产品健康、当前页面加载状态、数据完整度分开呈现。

## 12. 迁移策略

### 12.1 原则

- 先派生、后切换；
- 不破坏 v1.0 文件；
- 可重复运行；
- 每一步有对账；
- 任一步失败均保留旧系统可用；
- 无异盘备份时禁止破坏性生产迁移。

### 12.2 阶段

#### 阶段 0：备份与只读审计

- `daily-status`
- `backup-status`
- `health --max-age-hours 72`
- `doctor`
- `launchctl print`
- 外部便携备份和恢复校验
- 当前数据计数与哈希快照
- live 与 public 同名文件逐文件差异和归因
- CLI 注册命令与 wheel 文件闭包检查
- JSONL 行数、唯一 ID、SQLite ID 的双向对账
- 源前缀 SHA256 与 SQLite meta 验证
- staging 全量 reindex，成功后再原子换库
- 异盘备份，strict SHA256 全量通过
- 隔离 HOME 恢复后运行 v1.0 health、preflight、agent-context

当前 latest export 位于 vault 内或同盘、旧 manifest 只有 manifest-level 校验、存在凭证形态 warning 时，阶段 0 不通过。

v1.1 私人灾备 manifest 必须记录：

- 每个事件流 head sequence；
- 每个 current view 的 `based_on_event_seq`；
- schema version；
- Context、Claim、Judgment、Outcome 事件文件哈希；
- restore replay 结果。

私人灾备可以保留经过授权的私密事实，但必须位于用户控制的异盘位置。公开 GitHub 导出是另一条严格脱敏链路，二者不能共用「为了公开而删除私人历史」的规则。

#### 阶段 1：Claim 派生

- 读取旧 reviewed/profile 数据；
- 生成 Claim 候选；
- 归因审计；
- 对账计数；
- 不改变现有画像。

#### 阶段 2：Living Self 与 Judgment

- 生成 v1 候选；
- 与旧 `profile_nuwa` 对比；
- 标记冲突、缺失和低证据项；
- 用户确认后才成为 current。

#### 阶段 3：Context Compiler

- 双写旧 Context 和新 Context；
- 对比内容、隐私过滤和任务相关性；
- 新编译器通过回放后切换默认；
- 保留 `--legacy-context` 一个版本周期。

#### 阶段 4：产品 UI

- `/api/v2` 先上线；
- UI 消费真实 API；
- 浏览器验收后替换默认首页；
- 旧 Control Center 下沉到「系统」。

#### 阶段 5：生产切换

- 暂停破坏性维护；
- 重新备份；
- 安装 v1.1；
- 重建派生层；
- 启动服务；
- 执行生产验收；
- 保留 v1.0 回滚包。

### 12.3 回滚

回滚只需要：

1. 停止 v1.1 服务；
2. 将 live skill 切回 v1.0 安装包；
3. 恢复 v1.0 LaunchAgent 配置；
4. 忽略新增派生目录；
5. 验证原始 vault 哈希未变化；
6. 重新执行 v1.0 健康与恢复检查。

v1.1 新目录不影响 v1.0 读取，因此不需要删除。

## 13. 性能与容量

基线真实 vault 超过 470,000 条记录，所有实现以此量级设计。

要求：

- 首页 API 在预生成读模型存在时，P95 小于 500ms；
- 记忆分页 P95 小于 800ms；
- 单条详情 P95 小于 300ms；
- Living Self 页面 P95 小于 500ms；
- Context 预览在不做外部联网研究时，P95 小于 5s；
- 任何列表默认最多 50 条；
- API 不得一次返回全部证据；
- UI 首屏 JSON 建议小于 300KB，硬上限 1MB；
- 高成本模型重建放入后台 job；
- job 支持进度、取消、失败恢复和结构化日志。
- 47 万条规模下 SQLite 不可用时返回明确错误，不做 JSONL 全扫；
- 索引同步在源 prefix hash 变化时自动转 staging rebuild；
- staging rebuild 完成后要求 JSONL 唯一 ID 集与 SQLite ID 集一致。

## 14. 安全与隐私

### 14.1 本地服务

- 默认绑定 `127.0.0.1`；
- 拒绝非 loopback Host；
- 写请求必须精确匹配当前服务 scheme、host 和 port；
- 拒绝错误 Content-Type、缺 request ID、缺幂等键、缺 expected version 和超大请求；
- 禁止通配 CORS；
- 保持 CSP、`X-Frame-Options`、`nosniff`、`no-referrer` 和 `Cache-Control: no-store`；
- 静态资产拆分后 CSP 不允许 `unsafe-inline`；
- 写操作记录 request ID、动作、目标 ID、时间和结果，不记录私密正文。

### 14.2 输出

- 列表永远只返回摘要；
- 私密原文只在用户请求单条详情时经过脱敏返回；
- Context Pack 默认只包含摘要和证据 ID；
- 公开导出移除用户名、绝对路径、会话正文、open_id、token、cookie、客户名和本地数据；
- 表达 DNA 导出不携带证据正文。
- 隐私扫描覆盖源码、staged diff、wheel、sdist、ZIP 和最终 Release 资产内部；
- 扫描至少识别 open_id、Cookie、Bearer、URL userinfo、AWS key、绝对 Home 路径和私钥；

### 14.3 生产数据

- 测试默认使用临时 vault；
- 真实 vault 测试优先只读；
- 写入型生产验收使用专用测试命名空间；
- 清理测试命名空间前先核对 scope；
- 任何迁移不使用 `rm -rf`。

### 14.4 本地完整性威胁边界

- v1.1 的事件重放、快照摘要和跨流绑定用于检测截断、错序、单侧修改、部分写入及不一致重写，并在这些情况下失败关闭；
- v1.1 不宣称抵御已取得当前用户全部 vault 文件任意读写权限的恶意进程。此类攻击者可以同时重写多条本地权威日志，并按公开算法重算摘要；
- 抵御完全一致的双侧恶意重写需要 vault 之外的信任根，例如 macOS Keychain 保护的签名密钥、远端见证服务或只追加介质。该能力不在 v1.1 发布范围内，未来引入时必须单独完成密钥生命周期、恢复和离线可用性设计；
- 产品和验收报告不得把当前摘要机制描述为签名、HMAC 或对本机同账户恶意进程的防篡改保证。

## 15. 失败处理

| 场景 | 产品行为 |
|---|---|
| 归因不确定 | 保持候选，进入信任页，不进入 Living Self |
| 证据缺失 | 标记来源断裂，不把结论升级为确认 |
| 模型冲突 | 生成 tension 或版本差异，不静默覆盖 |
| Context 超预算 | 按相关性和证据强度裁剪，并展示排除原因 |
| 私密内容命中 | 排除正文，保留安全摘要或只保留 ID |
| 后台重建失败 | 旧 current 继续可用，显示失败和可重试 |
| API 写入重试 | 使用幂等键，返回原结果或冲突 |
| UI 刷新 | 从服务重读状态，不依赖浏览器内存 |
| 服务重启 | 未完成 job 标为 interrupted，可重试 |
| 新派生层损坏 | 从事件流或旧数据重建，不影响 raw vault |

## 16. 测试策略

### 16.1 单元测试

- 数据类型和 schema；
- 状态机；
- 归因规则；
- 作用域过滤；
- 隐私过滤；
- 置信度计算；
- 版本 diff；
- Context 预算和排序；
- 幂等事件；
- 错误协议。

### 16.2 集成测试

- 旧 profile 迁移到 Claim；
- Claim 到 Living Self；
- Judgment 结果回写；
- Context 预览到编译到结果；
- HTTP API 读写；
- job 进度和取消；
- 服务重启恢复；
- v1.0 CLI 兼容；
- 备份和恢复包含新增派生层。
- 中间回填、同尺寸重写和增尺寸重写触发 staging reindex；
- JSONL 与 SQLite ID 双向对账；
- CLI 注册目标在源树、wheel 和安装目录全部存在；
- orchestrator 必需阶段失败时 CLI 返回非零。

### 16.3 信任回归集

至少覆盖：

1. 他人评价不会变成用户自述；
2. 转述内容不会错认说话人；
3. 单次情绪不会变成长期人格；
4. 账号或角色不会串号；
5. 过期项目不会继续显示为当前事实；
6. 推断明确标记；
7. 反例能降低或阻断模型；
8. 修正保留历史；
9. 私密证据不会进入 Context；
10. 表达 DNA 不改变事实置信度。

### 16.4 模型评测

每个 Living Self 版本保留可重复测试：

- 3 个已知判断问题；
- 1 个边缘问题；
- 1 个反例问题；
- 1 个时间演化问题；
- 1 个作用域冲突问题。

评测维度：

- 立场一致性；
- 证据透明度；
- 边缘诚实度；
- 冲突处理；
- 作用域正确性；
- 隐私遵守；
- 表达辨识度，仅在显式启用时。

答题与评分必须分离。评分保存完整输入、输出、证据和 rubric，不只保存分数。最终生产门禁包含真实历史任务回放，不能只用合成题。

### 16.5 浏览器验收

使用真实本地服务和测试 vault 验证：

- 七个一级页面；
- 所有二级详情；
- 搜索、筛选、分页；
- 确认、纠正、拒绝和回滚；
- Context 预览、移除、编译、复制和结果记录；
- 刷新后状态持久化；
- 空状态、慢状态、错误状态；
- 1512px 桌面；
- 390px 移动；
- 键盘导航；
- 无脚本错误；
- 无横向滚动；
- `prefers-reduced-motion`。
- 深链接、刷新、前进和后退；
- 快速切页和慢响应不会发生旧响应覆盖；
- 精确同源、请求体上限、expected version 和幂等重试；
- CSP 不含 `unsafe-inline`；
- 47 万条规模的 keyset cursor 无重复、遗漏和深分页退化。

### 16.6 生产验收

上线后必须联合验证：

1. 版本号、CLI 和服务版本一致；
2. `daily-status`；
3. `backup-status`；
4. `health --max-age-hours 72`；
5. `doctor`；
6. `launchctl print` 当前标签；
7. `/readyz`；
8. 七模块 API；
9. 真实 Context 预览与编译；
10. 一条测试纠正的写入、刷新和审计；
11. 测试纠正回滚；
12. 外部备份恢复校验；
13. 真实浏览器桌面与移动验收；
14. 日志无凭证和私密正文。
15. JSONL 与 SQLite 记录 ID 完整对账。
16. live 与 public 漂移已逐文件处置。
17. 异盘备份和隔离恢复通过。
18. 当前 health、doctor 中的 Feishu mirror 错误已被证明修复或明确判定为可接受的实时边界。

## 17. 发布门禁

发布目标：`v1.1.0`

必须满足：

- `core/VERSION`、`pyproject.toml`、CLI、文档一致；
- 干净 clone 安装成功；
- 全量 pytest 通过；
- P0 回归 `8/8`；
- 新增 Living Self P0 回归全过；
- index JSONL 与 SQLite ID 集完全一致；
- event replay 后 current watermarks 一致；
- Python 3.9、3.10、3.11、3.12 CI 全绿；
- `scripts/private_scan.py .` 无命中；
- staged diff 人工逐文件检查；
- 公开包包含所有 CLI 引用文件；
- `cards` 兼容命令在干净安装中可用；
- `project`、`notes-sync` 等注册命令具有公开安全实现或已从所有入口移除；
- orchestrator 必需阶段失败返回非零；
- 真实 vault 不进入 Git；
- PR 合并；
- tag 指向 main 合并 commit；
- GitHub Release 版本、说明和资产一致；
- 本地安装版本与 release 一致。
- 最终资产由 main merge commit 构建，测试、扫描、生产安装和 Release 上传使用同一 SHA256；
- GitHub Release 下载资产再次安装、扫描和命令闭包验证通过。

## 18. 实施分解

### 里程碑 A：可信数据内核

- 索引完整性、包命令闭包、备份迁移门；
- 类型、事件流、Claim、归因、迁移、信任 API；
- 不改变默认 UI；
- 完成信任回归集。

### 里程碑 B：Living Self 与 Judgment

- 八模块模型；
- 版本、差异、纠正和回滚；
- Judgment Memory；
- 修复 `cards` 发布漂移。

### 里程碑 C：Context 使用闭环

- v2 Context Compiler；
- 预览；
- 隐私和预算；
- consume 与 outcome；
- 新旧 Context 对比和默认切换。

### 里程碑 D：真实产品看板

- 七模块 UI；
- 全部真实 API；
- 二级内容；
- 真实交互持久化；
- 桌面、移动、无障碍和错误状态。

### 里程碑 E：生产与发布

- 真实数据只读迁移；
- 生产切换；
- 全量验收；
- 脱敏；
- GitHub PR、tag、release；
- v1.0 回滚包验证。

每个里程碑独立提交，必须先测试再进入下一个里程碑。生产切换只在 A 至 D 全部通过后执行。

## 19. 验收定义

v1.1 只有在以下陈述全部有当前证据时才算完成：

1. 用户能看到今天新增的真实记忆。
2. 用户能看到系统对自己的理解变化及证据。
3. 用户能纠正模型，刷新后仍保留，历史版本仍可查看。
4. 他人观点、角色和账号不会污染用户画像。
5. 用户能查看和复用历史判断及结果。
6. 用户能预览 Context Pack 使用了什么、排除了什么。
7. Agent 获得的是任务相关、受预算和隐私约束的上下文。
8. 用户能记录本次调用结果，系统生成可审阅反馈。
9. 看板所有一级和二级页面均由真实 API 驱动。
10. 系统运行、备份、恢复和服务状态仍然可靠。
11. 干净安装不存在缺文件命令。
12. 公开 GitHub 版本与本地生产版本一致且脱敏。

任何一项只有计划、静态页面、HTTP 200、间接推断或缺少真实运行证据，都不算完成。
