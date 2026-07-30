# Immortal Memory 当前状态

更新时间：2026-07-31，Asia/Shanghai
当前仓库待发布版本：1.3.4
本机生产安装：仍为 1.3.3，本轮未部署、未重启、未读写真实 vault
本轮层级：打包配置修复已产出，远端发布以 `v1.3.4` tag、Release 与 CI 为准

## 1.3.4 源码验收

- 显式声明 `agents`、`product_assets` 与 `product_assets/views` 三个分发包，Setuptools 构建不再报告 package discovery warning。
- Homebrew Python 3.11 隔离环境全量回归：`1333 passed in 73.62s`。
- 打包与版本聚焦回归：8 项通过；源码隐私扫描为 `private_scan=ok`。
- 构建产物：wheel 651,966 bytes，SHA256 `d4dced3da862fd6349d394771ff8a53535ed73b4d8f5cf8325f89ec3581bb152`；sdist 796,926 bytes，SHA256 `b43cc6c05b75982ec3252f594c3dff668afe0d9a0bacfa02d587415419b1543b`。
- 本轮没有安装到本机生产目录，没有读取或写入真实 vault，没有重启控制中心或调度器。

## 1.3.3 已完成能力

- 修复健康检查继续读取退役 Markdown 卡片缓存而产生的假告警，判断力健康状态现在读取当前编排状态。
- 移除 Agent 上下文对退役、未审核卡片文件的直接注入，改为使用 Judgment Store 的当前数据和评估记录。
- 补齐顶层 `agent-context` 命令的预览、审核、编译参数，真实跑通 `preview` 到 `compiled` 生命周期。
- 修正 Control Center 展示的 Agent 模式与后端可接受模式不一致的问题，无效模式会在入队前返回 `400`。
- 更新 Codex、Claude Code 适配器和使用文档，明确 Agent 只能消费已编译上下文，不能把预览当成已批准记忆。
- Claude Desktop 的 MCP 配置已指向正式安装目录，不再指向旧版私有 skill 副本。
- MCP 握手版本改为读取产品统一版本，当前正确报告 `1.3.3`。
- 首页待确认数量改为报告真实总数，不再把八条展示上限误报为全部数量。
- Trust 页增加候选理解和候选判断的真实审核入口，长证据分类改为原生折叠面板。
- 新增 owner-only `learning-review` 命令，默认只生成脱敏预览；飞书发送固定使用本机配置中的本人 open_id，并要求显式远端写入确认。
- Context 结果表单现在能把已编译记忆逐条标为「确认为有用」或「标记为需复核」，真实写入 `confirmed_refs` 和 `challenged_refs`。
- Trust 账本新增「任务结果提出复核」，从 Outcome Store 读取被挑战的精确记忆引用，但不会自动改写 Claim 或 Judgment 权威。
- 已记录的 Context 结果会展示支持与需复核的引用数量，用户能看到反馈是否真正落库。
- 本机核心、Codex 适配器、Claude Code 适配器和控制中心已更新到同一份源码。

## 1.3.3 生产验收证据快照

- 全量测试：Python 3.11 下 `1332 passed in 78.75s`。
- Python 3.13 临时环境先得到 `1331 passed`，仅隔离 venv 的 `ensurepip` 因解释器自身 `SIGABRT` 失败；同一测试及全量套件已在支持环境 Python 3.11 通过。
- 学习审核、产品数据、UI、HTTP、打包和版本聚焦回归：`145 passed`。
- 产品数据、HTTP 与 UI 聚焦回归：`168 passed`。
- 聚焦回归：`51 passed`，新增 MCP 版本回归单测单独通过。
- 隐私扫描：`private_scan=ok`。
- 构建产物：`immortal_memory-1.3.3-py3-none-any.whl` 构建成功，源码和 wheel 隐私扫描均为 `private_scan=ok`。
- CLI：`immortal 1.3.3`。
- 真实数据健康：772,017 条记录，最近自动采集、清洗、蒸馏、画像、索引、关系和质量步骤均为当前状态，质量分 100。
- Agent 上下文：真实任务预览成功，同一预览经 ID 和哈希确认后编译成功，生成 `compiled` 上下文。
- MCP：正式安装路径握手成功，提供 `immortal_agent_entry`、`immortal_agent_context`、`immortal_recall` 三个工具。
- 控制中心：`http://127.0.0.1:8765/` 正常响应，版本为 `1.3.3`；服务已重启到新核心，每日调度仍为 loaded。
- 浏览器验收：Trust 账本真实显示「结果复核 0」和「任务结果提出复核」分类，不再把缺少字段渲染为「未知」。
- 浏览器验收：首页显示 57 条待确认、当前展示 8 条；Trust 页高度由约 10,237 px 收敛至 1,359 px，审核按钮跳转成功，控制台无错误。
- 学习审核真实预览：识别 57 条候选理解、0 条候选判断，默认展示 8 条；飞书 CLI `dry-run` 成功，未实际发送、未更改候选状态。

## 仍需关注

- 当前 7.0 GB 备份在同一磁盘。18,058 个文件的严格 SHA256 校验没有缺失或不一致，但这不等于灾难恢复保护。必须由用户指定外置盘或可信同步目录后才能完成外部备份。
- 本机当前没有可写外置盘或系统同步盘。飞书机器人身份可用，但本机没有 GPG 私钥，尚不能安全生成、上传并演练加密恢复包。
- 备份扫描发现 48 个凭证形态候选。系统只报告数量，不应自动改写不可变原始记忆；外发任何备份前必须先建立隔离和脱敏策略。
- Trust 层有 57 条候选需要本人确认，Living Self 当前已确认条目为 0，Judgment Store 当前为 0。系统不能自行把推断升级为用户身份事实。
- Claude Desktop 配置已修正，但已运行的 Claude MCP 子进程仍是旧路径。退出并重新打开 Claude Desktop 后才会切换到正式核心；本轮未强杀应用，避免打断正在进行的任务。
- `MAINTENANCE_FREEZE_DESTRUCTIVE` 仍保留。没有经过恢复演练和外部备份前，不解除破坏性维护冻结。
- GitHub 公开发布已获得明确授权，目标版本为 `v1.3.3`；远端是否完成以 GitHub tag、Release 和 CI 结果为最终证据。

## 下一步门槛

1. 指定外置盘或同步目录，生成并验证外部备份，使 `loss_protection` 从 `unprotected` 变为 `protected`。
2. 在 Trust 看板中人工确认或拒绝候选记忆，生成第一版有依据的 Living Self。
   可先运行 `immortal-memory learning-review` 本地预览；需要本人飞书提醒时，再显式运行 `immortal-memory learning-review --send-feishu --confirm-remote-write`。
3. 重启 Claude Desktop，验证实际 Claude 会话调用的是正式 MCP 核心。
4. 发布后核对 GitHub `v1.3.3` tag、Release、wheel 附件和远端 CI，不把本地推送成功单独当作公开发布完成。
