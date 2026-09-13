# SINGLE-SOURCE：Immortal Memory 真相源地图（2026-09-11 收拢后）

本文件是日常维护、升级、迭代的唯一入口。改动任何 immortal 相关代码前，先读这里。

## 1. 唯一标准版本

| 层 | 仓库/路径 | 版本 | 说明 |
|---|---|---|---|
| **v1 产品线（现役）** | `~/Documents/开源项目/repos/immortal-memory`（main，origin=github.com/HeiGeAi/immortal-memory） | **1.4.0a1** | 采集、五层架构（Claim/Living Self/Judgment/Context/Outcome）、控制中心、Agent Bridge |
| **v2 产品线（下一代）** | `~/immortal-memory`（main，origin=github.com/HeiGeAi/immortal-memory-v2 私有仓） | **2.0.0** | 判断卡/事实卡/人物卡三层精炼 + L2 供给层（MCP/hook），Claude Code 已接入 |

两条线是不同代际的独立代码库，各自只有一个真相源。任何改动必须先落仓库，再部署。

## 2. 部署关系（只允许从仓库流出）

- 运行时安装副本：`~/.local/share/immortal-memory/core/`，内容必须等于 v1 仓库 `core/`。同步命令：
  `rsync -a --delete --exclude='__pycache__' --exclude='*.bak-*' $REPO/core/ ~/.local/share/immortal-memory/core/`
- Agent 适配器（skill）：全部是指向 v1 仓库 adapters/ 的软链，禁止再放实体副本：
  - `~/.codex/skills/immortal-memory` → `adapters/codex/skills/immortal-memory`
  - `~/.claude/skills/immortal-memory` → `adapters/claude-code/skills/immortal-memory`
  - `~/.agents/skills/immortal-memory`、`~/.skills-manager/skills/immortal-memory-2` → `adapters/codex/skills/immortal-memory`
- v2 接入：Claude Code hooks（`~/.claude/settings.json` → `bin/hook.py`）+ MCP（`~/.claude.json`）直接指 v2 仓库路径，v2 仓库即运行副本。

## 3. 数据层

- vault：`~/.immortal/`（index.jsonl 约 2.27GB / 110 万条，daily/ 采集归档，v2 的 cards.db/corpus.db 在 `~/.immortal/v2/`）。数据永不进任何远端仓库，git 只放脱敏白名单快照。
- 备份：`~/.immortal-backups/authored-events/` + exports 轮换（`mem prune`，保留 2 份）。

## 4. 调度（launchd，无 crontab）

launchd 标签实际以本机用户名为用户段（表中统一写作 com.example.* 脱敏）。

| 标签 | 内容 | 归属 |
|---|---|---|
| com.blake.immortal.daily-backup | `immortal.py run` 4 时段采集 + feedback 通知 | v1 |
| com.example.immortal.daily-health-check | status/doctor/health 三连检 | v1 |
| com.example.immortal.feishu-mirror-worker | 飞书 Drive 镜像 run-once | v1 |
| com.example.immortal.profile-review | 8765 端口审阅台 | v1 |
| com.immortal-memory.v2.prune | `mem daily`（v2 备份 + exports 轮换） | v2 |

## 5. 日常维护流程

1. 改代码：只在上述两个仓库改，跑全量测试（v1：`PYTHONPATH=core /usr/bin/python3 -m pytest tests/ -q`，当前 1337 passed；v2：`.venv/bin/python -m pytest tests/ -q`）。
2. 发版：main 打 tag（如 v1.4.0a1），GitHub tag + Release 为发布完成证据。
3. 部署：按第 2 节 rsync/软链，改完 `immortal.py --version` 核对，重启 `launchctl kickstart -k gui/501/com.example.immortal.profile-review`。
4. 禁止：直接改 `~/.local/share/` 或 skill 安装位的内容（它们只是部署产物）。

## 6. 已清理项（2026-09-11）

- 死代码 `~/.codex/skills/immortal`、`~/.claude/skills/immortal`（5 月版，桥接断链）已归档至 `~/.immortal-backups/consolidation-20260911/archived/`。
- config.json 的 git 采集源已移除死目录。
- 历史克隆：`~/claudecode/immortal-release-work/immortal-memory`（落后 main 144 commit，WIP 已被主线取代）与 `Documents/Codex/黑哥 AI/github-fixes-20260830/worktrees/immortal-memory`（3 个修复已进 main）仅作历史参考，不再维护。
- 分支 `codex/update-readme-v1.3.3`（claim governance 半成品，曾破坏 test_v11_migration）保留在远端待重做，其演进续作已含于 1.4.0a1。
