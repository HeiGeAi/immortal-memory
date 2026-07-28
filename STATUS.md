# Immortal Memory 当前状态

更新时间：2026-07-28 12:34 CST
当前本地版本：1.3.2
本轮层级：本地源码已验证，本机真实安装已验证，GitHub 尚未发布

## 已完成

- 修复健康检查继续读取退役 Markdown 卡片缓存而产生的假告警，判断力健康状态现在读取当前编排状态。
- 移除 Agent 上下文对退役、未审核卡片文件的直接注入，改为使用 Judgment Store 的当前数据和评估记录。
- 补齐顶层 `agent-context` 命令的预览、审核、编译参数，真实跑通 `preview` 到 `compiled` 生命周期。
- 修正 Control Center 展示的 Agent 模式与后端可接受模式不一致的问题，无效模式会在入队前返回 `400`。
- 更新 Codex、Claude Code 适配器和使用文档，明确 Agent 只能消费已编译上下文，不能把预览当成已批准记忆。
- Claude Desktop 的 MCP 配置已指向正式安装目录，不再指向旧版私有 skill 副本。
- MCP 握手版本改为读取产品统一版本，当前正确报告 `1.3.2`。
- 首页待确认数量改为报告真实总数，不再把八条展示上限误报为全部数量。
- Trust 页增加候选理解和候选判断的真实审核入口，长证据分类改为原生折叠面板。
- 新增 owner-only `learning-review` 命令，默认只生成脱敏预览；飞书发送固定使用本机配置中的本人 open_id，并要求显式远端写入确认。
- 本机核心、Codex 适配器、Claude Code 适配器和控制中心已更新到同一份源码。

## 验收证据

- 全量测试：`1331 passed in 84.69s`。
- 学习审核、产品数据、UI、HTTP、打包和版本聚焦回归：`145 passed`。
- 产品数据、HTTP 与 UI 聚焦回归：`168 passed`。
- 聚焦回归：`51 passed`，新增 MCP 版本回归单测单独通过。
- 隐私扫描：`private_scan=ok`。
- 构建产物：`immortal_memory-1.3.2-py3-none-any.whl` 构建成功。
- CLI：`immortal 1.3.2`。
- 真实数据健康：772,017 条记录，最近自动采集、清洗、蒸馏、画像、索引、关系和质量步骤均为当前状态，质量分 100。
- Agent 上下文：真实任务预览成功，同一预览经 ID 和哈希确认后编译成功，生成 `compiled` 上下文。
- MCP：正式安装路径握手成功，提供 `immortal_agent_entry`、`immortal_agent_context`、`immortal_recall` 三个工具。
- 控制中心：`http://127.0.0.1:8765/` 正常响应，版本为 `1.3.2`；服务、调度、最近运行和自动反馈均健康。
- 浏览器验收：首页显示 57 条待确认、当前展示 8 条；Trust 页高度由约 10,237 px 收敛至 1,359 px，审核按钮跳转成功，控制台无错误。
- 学习审核真实预览：识别 57 条候选理解、0 条候选判断，默认展示 8 条；飞书 CLI `dry-run` 成功，未实际发送、未更改候选状态。

## 仍需关注

- 当前 7.0 GB 备份在同一磁盘。18,058 个文件的严格 SHA256 校验没有缺失或不一致，但这不等于灾难恢复保护。必须由用户指定外置盘或可信同步目录后才能完成外部备份。
- 本机当前没有可写外置盘或系统同步盘。飞书机器人身份可用，但本机没有 GPG 私钥，尚不能安全生成、上传并演练加密恢复包。
- 备份扫描发现 48 个凭证形态候选。系统只报告数量，不应自动改写不可变原始记忆；外发任何备份前必须先建立隔离和脱敏策略。
- Trust 层有 57 条候选需要本人确认，Living Self 当前已确认条目为 0，Judgment Store 当前为 0。系统不能自行把推断升级为用户身份事实。
- Claude Desktop 配置已修正，但已运行的 Claude MCP 子进程仍是旧路径。退出并重新打开 Claude Desktop 后才会切换到正式核心；本轮未强杀应用，避免打断正在进行的任务。
- `MAINTENANCE_FREEZE_DESTRUCTIVE` 仍保留。没有经过恢复演练和外部备份前，不解除破坏性维护冻结。
- GitHub 公共仓库尚未更新。本轮没有获得新的公开发布确认，因此没有提交、推送或打标签。

## 下一步门槛

1. 指定外置盘或同步目录，生成并验证外部备份，使 `loss_protection` 从 `unprotected` 变为 `protected`。
2. 在 Trust 看板中人工确认或拒绝候选记忆，生成第一版有依据的 Living Self。
   可先运行 `immortal-memory learning-review` 本地预览；需要本人飞书提醒时，再显式运行 `immortal-memory learning-review --send-feishu --confirm-remote-write`。
3. 重启 Claude Desktop，验证实际 Claude 会话调用的是正式 MCP 核心。
4. 用户明确确认公开发布后，再提交、推送 `1.3.2` 并更新 GitHub Release。
