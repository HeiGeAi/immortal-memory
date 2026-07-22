# Immortal Memory · 赛博永生记忆库

> 给本地 AI 一个能长期记住你、并且按你的方式思考的记忆底座。

<div align="center">

![Version](https://img.shields.io/badge/version-v1.1.1-111827.svg)
![Python](https://img.shields.io/badge/python-3.9%2B-3776AB.svg?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/platform-Codex%20%7C%20Claude%20Code%20%7C%20Local%20Agent-0F766E.svg)
![License](https://img.shields.io/badge/license-MIT-059669.svg)

[我为什么做这个](#我为什么做这个) · [快速开始](#快速开始) · [让任何-agent-用上它](#让任何-agent-用上它) · [架构](./docs/ARCHITECTURE.md) · [隐私边界](./docs/PRIVACY.md) · [English](#immortal-memory-english)

</div>

---

## 我为什么做这个

模型已经够聪明了。

现在卡住产出质量的，不是模型本身，是模型对你了解多少。

我把这个判断叫做 AI 正在从 **CPU Bound 走向 Memory Bound**。算力和智能跨过某个阈值之后，你再换一个更强的模型，拿到的东西也只是更流畅的废话。真正决定它能不能帮到你的，是它手上关于你的上下文有多密。

还有一个更要命的问题。大模型天生爱说**正确的废话**。它的底层是 next token prediction，再叠上 RLHF，本质就是在奖励共识、惩罚异见。所以你不喂它东西，它默认给你的就是全网平均水平的那套话。

要让它说出像你会说的话、按你会做的判断去做事，只有一个办法：用**足够高密度的个人上下文**，把它从共识里硬拽出来。

Immortal Memory 就是干这件事的本地系统。它不是一句 prompt，也不只是一个 Codex 技能。v1.1 用五个可追溯层完成这件事：

1. **Claim**：把原始痕迹变成带出处、作用域、归因和隐私标签的可纠正主张。
2. **Living Self**：只用已确认 Claim 生成有版本的「当前自我」，旧版本永不原地覆盖。
3. **Judgment**：保存情境、选择、结果和教训，不把一次选择夸大成永久原则。
4. **Context**：按当前任务预览并编译有界上下文，用户能看到、排除和确认实际交给 Agent 的内容。
5. **Outcome**：记录使用结果，让后续判断有真实反馈，而不是只有画像没有闭环。

采集、检索和角色编译仍然存在，但它们围绕这五层工作。每次纠正都会保留旧 Claim，写入新的纠正事件，并生成新的 Living Self 版本，因此「系统现在怎么理解你」和「它为什么改变」都能追溯。

## 为什么不是再写一份长 prompt

很多人理解的让 AI 记住我，是把我喜欢 TypeScript、我说话简洁这类事实塞给它。

这只能让它知道你是谁，不能让它像你一样思考。

**事实层和判断原则层是两回事。**

- 事实层：你偏好什么。
- 判断原则层：你在可维护性和性能之间到底怎么权衡，你凭什么拒绝一个看着不错的机会。

后者才是你区别于别人的地方，也是 AI 最学不会的地方。所以这套系统采集的重点，不是你写过的漂亮 prompt，而是你的**客观行为**：

- 录音转写、会议记录、对话导出
- 每一次你纠正 AI 的记录（这个最值钱，它暴露你真实的判断标准）
- 你的决策过程、权衡理由、失败复盘

一个关键原则：采集真实的你，不是理想中的你。你实际怎么做决策，比你以为自己怎么做决策，重要得多。

## 它怎么把噪音变成判断

三层往上走，越往上越稳定。

- **L1 观察层**：每天扫一遍，提取当天的观察和决策。
- **L2 反思层**：每周合并去噪，找出跨项目反复出现的模式。
- **L3 公理层**：蒸馏出真正稳定的判断原则。

筛选标准只有一个：**稳定性**。跨场景、跨时间反复出现的东西，才配进 L3。一次性的情绪和临时想法，留在下面就够了。

## 边界：它能代理你，但不冒充你

它可以：

- 帮你回忆有出处的证据
- 复刻你的表达偏好
- 替你预判常规决策
- 起草和审稿
- 告诉你它凭什么得出这个结论

它有几条硬线：

- 不经你同意，不做不可逆的决定
- 默认不把私聊原文吐出来，只给摘要和证据编号
- 不会悄悄采集错的账号
- 不会把一次幻觉出来的画像当成永久真相

数据默认全在本地。采集范围、身份别名、账号护栏、导出、删除，全部显式可控。

## 这个仓库是什么

这是 Immortal Memory 的**公开空壳版**，只有代码、适配器和模板。

它不包含任何人的私人记忆库、聊天记录、文档、生成的画像、角色证据、日志或密钥。这些东西属于你本地的 `~/.immortal/`，永远不进仓库。

## 快速开始

```bash
git clone https://github.com/HeiGeAi/immortal-memory.git
cd immortal-memory

# 安装，并装上 Codex 适配器
python3 install.py --owner-display-name "Your Name" --alias "Your Alias" --install-codex-adapter

# 跑一遍冒烟训练，顺手编译一个写稿审稿角色
immortal-memory train --smoke --build-role --goal "writing review" --mode writer

# 看看给 agent 的入口长什么样
immortal-memory agent-entry

# 针对一个具体任务，生成贴身的上下文包
immortal-memory agent-context "help me review this product idea" --print
```

打开本地控制台：

```bash
immortal-memory agent-factory
```

然后访问 http://127.0.0.1:8765/

看板有七个真实模块：**首页、记忆、我、判断、使用、信任、系统**。首页回答今天新增了什么价值；「记忆」追溯原始证据；「我」展示 Living Self 的八个认知分区；「判断」管理判断卡；「使用」预览并编译 Context Pack；「信任」解释归因、隐私排除和纠正；「系统」展示采集、索引、备份、服务与版本健康。

### 从 v1.0 做隔离迁移演练

下面这组命令只完成隔离恢复和迁移演练，不会切换生产。不要直接在生产 vault 上试跑。先创建异盘备份并通过严格恢复校验，再把 v1.0 vault 恢复到独立 staging 目录。含无时区 Hermes 时间戳时，必须先准备经哈希绑定的私有时区合同，并把参数同时传给 staging 和 migration：

```bash
immortal-memory backup --vault-dir "$HOME/.immortal" --output-dir "/Volumes/IMMORTAL_BACKUP/immortal-v1.0" --redact-secrets --fail-on-secrets
EXPORT_DIR="$(find "/Volumes/IMMORTAL_BACKUP/immortal-v1.0" -maxdepth 1 -type d -name 'immortal-export-*' | sort | tail -n 1)"
immortal-memory restore-check "$EXPORT_DIR" --strict --json
python3 core/export_restore.py restore-export "$EXPORT_DIR" "/tmp/immortal-v11-staging" --rebind-vault-config
TIMEZONE_CONTRACT="/absolute/path/to/hermes-timezone-contract.json"
python3 core/export_restore.py stage-v11-index --vault-dir "/tmp/immortal-v11-staging" --timezone-contract "$TIMEZONE_CONTRACT"
python3 core/export_restore.py v11-migrate --vault-dir "/tmp/immortal-v11-staging" --timezone-contract "$TIMEZONE_CONTRACT"
```

`--redact-secrets` never edits the authoritative vault. It creates a deterministic
credential-redacted `index.jsonl` inside the backup generation, records full
SHA-256 evidence in `secret-redaction-receipt.json`, scans the exported copy
again, and makes `backup` use strict restore verification. This option is scoped
to the memory index. It is not a claim that arbitrary binary or unrelated vault
files have been secret-scanned.

如果旧 `index.jsonl` 含无时区时间戳，`stage-v11-index` 会隔离无法证明时区的记录并拒绝生产切换。只有来源可信、权限为 `0600`、内容哈希匹配的 Hermes 时区合同才能通过 `--timezone-contract /absolute/path/to/contract.json` 提供证据。不要为了让门禁变绿而猜时区。

这一步产生的 `index.staging.jsonl` 只用于审计和隔离报告，不能当成事实源或直接发布。`v11-migrate` 会从未改写的原始 `index.jsonl` 重建 schema-v3 SQLite；合同只允许把已证明的 Hermes 本地墙钟时间写入派生字段 `ts_utc`，不会改写原始时间戳。正式切换必须走生产发布流程：从已合并 `main` 构建唯一 wheel，在隔离 vault 完成重建，运行 `prewarm_index_verification`，再以 `v11_production_switch_gate` 同时绑定迁移报告、原始 source 哈希、数据库 generation 和 staging receipt。只有 `production_switch_allowed=true` 才能运行模型阶段并安装同一 wheel 到真实环境；之后还要核对原始哈希、真实 API、浏览器、外置备份和 v1.0 回滚包。仓库目前故意没有提供绕过这些门禁的一键覆盖命令。

v1.1 先派生、后切换，不改写 v1.0 原始文件。需要回滚时，停止 v1.1 服务，恢复 v1.0 安装包和 LaunchAgent，忽略 v1.1 新增派生目录，然后核对原始 vault 哈希并重新运行 v1.0 健康检查。

### 飞书网盘异地恢复

飞书网盘可以作为额外的异地恢复介质，但它不是同步盘替代品。只有「加密上传后，再从远端下载、解密、严格校验并恢复到隔离目录」的演练成功，才会在迁移门禁中被当作外部恢复证据。

以下命令只使用你自己的 vault、你已经安装的 GPG 公钥和你自己拥有的飞书文件夹。尖括号内容必须替换成真实的公开指纹或用户自有文件夹 token，绝不要提供私钥、不要把包上传到网盘根目录或不受信任的共享文件夹。

```bash
# 1. 生成仅用于恢复的、已做凭证形态脱敏的本地导出。
immortal-memory backup \
  --vault-dir "$HOME/.immortal" \
  --output-dir "$HOME/.immortal/recovery/exports" \
  --redact-secrets --fail-on-secrets

# 2. 用已经存在的 GPG 公钥指纹构建本地加密包。此步不会访问飞书。
immortal-memory feishu-recovery prepare \
  --export-dir "$HOME/.immortal/recovery/exports/immortal-export-<timestamp>" \
  --package-dir "$HOME/.immortal/recovery/packages/<package-id>" \
  --recipient <PUBLIC_KEY_FINGERPRINT>

# 3. 显式确认后才写入飞书。父文件夹必须是你专门创建并拥有的目录。
immortal-memory feishu-recovery upload \
  --package-dir "$HOME/.immortal/recovery/packages/<package-id>" \
  --parent-folder-token <USER_OWNED_FOLDER_TOKEN> \
  --vault-dir "$HOME/.immortal" \
  --confirm-remote-write

# 4. 从远端下载所有密文分片，用匹配的私钥解密并恢复到临时隔离 vault。
immortal-memory feishu-recovery drill \
  --vault-dir "$HOME/.immortal" \
  --receipt "$HOME/.immortal/recovery/feishu/receipts/<package-id>.upload.json"

# 5. 只有演练收据仍绑定当前 index.jsonl 时，才允许把它作为迁移外部备份。
immortal-memory migration-preflight \
  --require-external-backup \
  --backup-source feishu-cloud \
  --json
```

`prepare` 只创建本地密文分片，不上传。`upload` 会向飞书写入私有加密数据，必须带 `--confirm-remote-write`。`drill` 需要对应私钥，它会下载、核对哈希、解密、严格校验导出并完成隔离恢复，临时明文随后删除。上传成功、本地收据、飞书同步状态或没有当前演练的绿色看板都不能证明灾备可恢复。

### v1.0 命令迁移

`immortal package` 已安全退役。公开发布只能从干净 Git commit 构建 wheel 和 source archive，并分别扫描源码、wheel、archive 和 adapter。不要从真实运行目录复制文件，也不要用姓名、路径或客户名替换表冒充脱敏。

旧版 `immortal project` 是绑定个人 Obsidian 目录、旧索引接口和旧卡片格式的本地工作流，不属于 v1.1 七模块公共产品。需要这项能力时，应把审查后的扩展保存在 Git 外，通过独立的 `immortal-project` 命令运行。公开 wheel 不会从私有 vault 动态加载 Python。显式调用两个旧命令会返回稳定迁移提示，不会读取 HOME 或创建文件。

### 生产健康与全新安装验收

```bash
immortal-memory daily-status
immortal-memory feedback --print
immortal-memory backup-status --verify --max-age-hours 168 --json
immortal-memory health --max-age-hours 72
immortal-memory doctor
immortal-memory preflight
immortal-memory agent-context "release acceptance" --print
```

系统看板会把主流程、自动反馈和本机通知分开显示。`run` 成功不等于整条自动化成功：飞书来源部分失败或通知未送达时，反馈卡和调度器都会明确标为需要关注。

全新安装应在隔离的 `HOME` 中从 wheel 安装，执行 `init`、`train --smoke`、上述健康命令和一次 `agent-context`，确认没有借用旧机器的 vault 或配置。v1.0 的读取、健康检查、Agent Bridge 和旧 Control Center 在 v1.1 中保留一个版本周期；新目录是派生层，v1.0 可以安全忽略。

```bash
CLEAN_HOME="$(mktemp -d /tmp/immortal-clean-home.XXXXXX)"
python3 -m venv "$CLEAN_HOME/venv"
WHEEL="$(find "$(pwd)/dist" -maxdepth 1 -name 'immortal_memory-1.1.1-*.whl' | head -n 1)"
HOME="$CLEAN_HOME" "$CLEAN_HOME/venv/bin/python" -m pip install "$WHEEL"
HOME="$CLEAN_HOME" "$CLEAN_HOME/venv/bin/immortal-memory" init --owner-display-name "Clean Install" --alias "clean"
HOME="$CLEAN_HOME" "$CLEAN_HOME/venv/bin/immortal-memory" train --smoke
HOME="$CLEAN_HOME" "$CLEAN_HOME/venv/bin/immortal-memory" health --max-age-hours 72
HOME="$CLEAN_HOME" "$CLEAN_HOME/venv/bin/immortal-memory" preflight
HOME="$CLEAN_HOME" "$CLEAN_HOME/venv/bin/immortal-memory" agent-context "clean install acceptance" --print
```

空白 vault 尚未配置外置备份或每日调度时，`health` 和 `preflight` 应明确返回待配置项，不能假装健康；clean-install 验收关注命令可运行、状态诚实、数据只写入隔离 `HOME`。生产验收则要求前述健康命令全部通过。

检索索引由每日采集编排中的 `index_db.py sync` 深度校验并同步。用户发起
`recall` 时只读取已验证的 SQLite 索引和固定数量的水位元数据，不会临时扫描
完整 `index.jsonl`、计算全文件摘要或触发重建。水位过期时查询会明确返回索引
不可用，等待下一次同步，而不是用可能过期的结果冒充健康。水位检查与 SQLite
查询持有同一个 source→database 共享快照锁，采集写入不能插进两者之间。

这里的 O(1) 可信水位建立在两个明确合同上：内置采集器都使用共同的 POSIX
advisory lock 写入 source；文件系统提供可靠的 `st_ctime_ns`，并与 dev、inode、
size、mtime 一起写入水位。外部手工改写即使恢复 mtime，也会因 ctime 失配而让
索引变为不可用。本实现依赖 `fcntl.flock`，当前支持 macOS 和 Linux，不支持
Windows 原生运行。

## 让任何 agent 用上它

给本地 agent 一段这样的交接说明就行：

```text
先读 ~/.immortal/agent/ENTRY.md。然后运行：
immortal-memory agent-context "<当前任务>" --print
把返回的内容当作任务级记忆来用，默认不要直接去读原始库。
```

这套模式对下面这些都成立：

| 平台 | 接入方式 |
|---|---|
| Codex | 装 `adapters/codex/skills/immortal-memory` |
| Claude Code | 装 `adapters/claude-code/skills/immortal-memory` |
| 通用 CLI agent | 直接跑 `immortal-memory agent-context` |
| MCP / HTTP | 通过 Agent Bridge 扩展（规划中） |

## 项目结构

```text
immortal-memory/
├── install.py            # 一键安装 + 绑定身份
├── core/                 # 本地记忆引擎和控制台
│   ├── immortal.py       # CLI 入口
│   ├── agent_bridge.py   # agent 桥接
│   ├── collect.py        # 多源采集
│   ├── profile.py        # 长期画像
│   └── role_distill.py   # 角色编译
├── adapters/
│   ├── codex/            # Codex 适配器
│   └── claude-code/      # Claude Code 适配器
├── docs/                 # 产品 / 架构 / 隐私 / 适配器文档
├── examples/             # prompt 和配置示例
└── scripts/
    ├── private_scan.py   # 发布前数据泄露扫描
    └── smoke_test.sh     # 冒烟测试
```

## 产品形态

```text
数据源 -> 本地库 -> 清洗/蒸馏 -> 画像/证据 -> agent 桥接 -> 各类适配器
```

## 数据策略

私人数据属于 `~/.immortal/`，不进这个仓库。在你 fork 出去发布之前，先跑一遍：

```bash
python3 scripts/private_scan.py .
python3 -m py_compile $(find core -maxdepth 1 -name '*.py')
```

## License

MIT。如果你的项目需要别的协议，发布前自己改掉。

---

<a id="immortal-memory-english"></a>

# Immortal Memory (English)

> A local-first memory layer that lets any AI agent remember you over the long run, and reason the way you actually reason.

## Why I built this

Models are already smart enough.

What caps the quality of what you get back is no longer the model. It is how much the model actually knows about you.

I think of this as AI moving from **CPU Bound to Memory Bound**. Once intelligence crosses a threshold, swapping in a bigger model just gives you more fluent boilerplate. What decides whether it can really help you is the density of your personal context in its hands.

There is a sharper problem underneath. A large model is wired to produce **confident, agreeable nonsense**. Next token prediction plus RLHF rewards consensus and punishes anything off-consensus. Feed it nothing and the default answer is the internet average.

To make it say what you would say and decide the way you would decide, there is only one move: push **high-density personal context** at it until it gets dragged out of the consensus prior.

Immortal Memory is the local system that does exactly that. It is not a prompt, and not only a Codex skill. v1.1 implements five traceable layers:

1. **Claim** turns source traces into correctable assertions with evidence, scope, attribution, and privacy labels.
2. **Living Self** builds a versioned current model from confirmed Claims without overwriting history.
3. **Judgment** preserves situation, choice, result, and lesson without turning one decision into a universal rule.
4. **Context** previews and compiles a bounded task pack so the user can inspect and exclude what an Agent will receive.
5. **Outcome** records what happened after use, closing the loop with real feedback.

Collection, retrieval, and role compilation remain, but now operate around these five layers. A correction preserves the old Claim, appends a correction event, and produces a new Living Self version, so every change remains explainable.

## Why not just write a longer prompt

Most people read "remember me" as feeding the model facts: I like TypeScript, I write concisely.

That tells it who you are. It does not make it think like you.

**Facts and judgment principles are two different layers.**

- Fact layer: what you prefer.
- Judgment layer: how you trade off maintainability against performance, why you turn down an opportunity that looks good on paper.

The second layer is what separates you from everyone else, and it is the part a model is worst at learning. So this system collects your **objective behavior**, not the polished prompts you once wrote:

- recordings, meeting notes, exported conversations
- every time you correct the AI (the most valuable signal, it exposes your real standards)
- your decisions, the reasoning behind the tradeoffs, your postmortems

One rule holds it together: capture the real you, not the ideal you. How you actually decide matters far more than how you think you decide.

## How it turns noise into judgment

Three layers, more stable as you go up.

- **L1 Observer**: a daily scan that pulls out the day's observations and decisions.
- **L2 Reflector**: a weekly merge and denoise that finds patterns recurring across projects.
- **L3 Axiom**: the distilled, durable judgment principles.

The single filter is **stability**. Only what recurs across contexts and across time earns a place in L3. One-off moods and passing thoughts stay below.

## Boundaries: it can act for you, not pose as you

It can recall evidence with sources, mirror your writing preferences, pre-judge routine decisions, draft and review, and explain why it reached a conclusion.

It holds a few hard lines: no irreversible decisions without your approval, no raw private messages by default (summaries and evidence IDs instead), no silent collection of the wrong account, and no treating a single hallucinated profile as permanent truth.

Data stays local by default. Collection scope, identity aliases, account guards, exports, and deletion are all explicit.

## What this repo is

This is the **public empty-shell** distribution: code, adapters, and templates only.

It contains no private vault, chat records, documents, generated profiles, role evidence, logs, or secrets. Those live in your local `~/.immortal/` and never enter the repository.

## Quick Start

```bash
git clone https://github.com/HeiGeAi/immortal-memory.git
cd immortal-memory
python3 install.py --owner-display-name "Your Name" --alias "Your Alias" --install-codex-adapter
immortal-memory train --smoke --build-role --goal "writing review" --mode writer
immortal-memory agent-entry
immortal-memory agent-context "help me review this product idea" --print
```

Open the local dashboard:

```bash
immortal-memory agent-factory
```

Then visit http://127.0.0.1:8765/

The dashboard has seven real modules: Home, Memory, Self, Judgment, Use, Trust, and System. v1.0 reading paths, health checks, Agent Bridge, and the legacy Control Center remain compatible for one release cycle. See [Architecture](./docs/ARCHITECTURE.md), [Product](./docs/PRODUCT.md), and [Privacy](./docs/PRIVACY.md) for migration, rollback, health, and privacy contracts.

The legacy `immortal package` command is retired in favor of release artifacts
built from a clean Git commit. The old personal Obsidian `immortal project`
workflow is available only as a separately reviewed local extension invoked as
`immortal-project`; it is not included in the public wheel and the runtime does
not load Python from the private vault.

## How other agents use it

Hand a local agent this:

```text
Read ~/.immortal/agent/ENTRY.md first. Then run:
immortal-memory agent-context "<current task>" --print
Use the returned context as task-local memory. Do not read the raw vault by default.
```

This works for Codex, Claude Code, terminal agents, and any tool that can read local files and run shell commands.

## Data policy

Private data belongs in `~/.immortal/`, not in this repo. Before publishing a fork:

```bash
python3 scripts/private_scan.py .
python3 -m py_compile $(find core -maxdepth 1 -name '*.py')
```

## License

MIT. Change it before publishing if your project needs a different license.
