# Immortal Memory 当前状态

更新时间：2026-09-11，Asia/Shanghai
当前仓库待发布版本：1.4.0a1
本机生产安装：1.4.0a1（安装副本与仓库核心同源，本轮完成「单一真相源」收拢）

## 1.4.0a1 收拢说明

- 本版本把本机生产运行时（2026-08-08 至 08-24 演进）收拢进仓库 main，作为后续唯一维护基线。
- 收拢内容：Context delivery receipt 与 acknowledge 生命周期、完整 MCP server（McpSession、协议协商、入参校验）、记忆工作台 UI（memories.js 重写）、memory value cohorts、claim history index v2、export_restore WAL/SHM 分文件签名校验、`python3 -B` 防 pyc 写入。
- 生产发布锁修复（state_store 发布锁原子性、进程存活锁保留）已在 main，运行时安装副本将随重装同步获得。
- 版本治理：pyproject、core/VERSION、README 徽章、STATUS 目标版本统一为 1.4.0a1。
- 隐私扫描：收拢前对安装副本全部 py/json/yaml/js 扫描密钥、个人邮箱、手机号、身份证模式，0 命中；`__pycache__` 与 `*.bak-*` 不入库。

## 仍需关注

- 外置备份仍未建立（同盘 exports 轮换不构成灾难恢复保护）。
- index.jsonl 单文件约 2.27GB、110 万条，仍为 append-only JSONL；分片或迁移到 SQLite 属于后续架构任务。
- 历史分支 `codex/update-readme-v1.3.3`（claim governance 半成品，曾破坏 test_v11_migration）保留在远端待重做；其演进续作已包含在 1.4.0a1 的 product_data/product_mutations 中。

## 下一步门槛

1. 用本仓库 main 重装本机运行时，保持「仓库即真相源」。
2. Trust 看板人工确认候选记忆，生成第一版有依据的 Living Self。
3. 指定外置盘或同步目录完成外部备份演练。
## 发布目标

- GitHub 公开发布目标版本为 `v1.4.0a1`；远端是否完成以 GitHub tag、Release 和 CI 结果为最终证据。
- 发布后核对 GitHub `v1.4.0a1` tag、Release、wheel 附件和远端 CI，不把本地推送成功单独当作公开发布完成。
