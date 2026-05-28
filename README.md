# Immortal Memory · 赛博永生记忆库

> 给本地 AI 一个能长期记住你、并且按你的方式思考的记忆底座。

<div align="center">

![Version](https://img.shields.io/badge/version-v0.1.0-111827.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-3776AB.svg?logo=python&logoColor=white)
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

Immortal Memory 就是干这件事的本地系统。它不是一句 prompt，也不只是一个 Codex 技能。它做四件事：

1. **先把你正在丢失的数字痕迹接住。** 对话、文件、会议、聊天记录，先存下来，丢不了。
2. **把这些原始痕迹蒸馏成可检索、有出处的记忆。** 每一条都能追回到源头。
3. **任何本地 AI agent 干活之前，给它生成一份贴着当前任务的上下文包。**
4. **需要重复某种行为时，直接编译出一个场景化的角色 agent。** 比如写稿审稿、商业顾问、项目操盘、会议分析。

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

Immortal Memory is the local system that does exactly that. It is not a prompt, and not only a Codex skill. It does four things:

1. **Catch your digital traces before they are lost.** Conversations, files, meetings, chat logs, all captured into a recoverable vault.
2. **Distill raw traces into searchable, evidence-backed memory.** Every memory points back to its source.
3. **Generate a task-local context pack** for any local agent before it starts work.
4. **Compile scenario role agents** when you need a behavior on repeat: writing reviewer, business advisor, project operator, meeting analyst.

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
