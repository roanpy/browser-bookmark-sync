<div align="center">
  <h1>Bookmark Sync</h1>
  <p><strong>安全、本地优先的 macOS 多浏览器书签镜像工具。</strong></p>

  [English](README.md) · 简体中文
</div>

Bookmark Sync 是一个命令行工具和轻量 macOS 应用，可手动指定 Chrome、Edge、Safari、Brave、Vivaldi、Opera 之间的书签同步方向。书签数据只在本机处理，项目不提供账号系统或云端书签服务。

## 为什么会有这个项目

我一直会在多个浏览器和不同使用环境之间切换，但浏览器书签通常只能在同一家厂商的生态内比较可靠地同步。要让 Chrome、Edge、Safari 以及其他浏览器保持同一套书签，实际并不容易；尤其是在开启浏览器原生云同步后，本地修改可能暂时正确，浏览器重新打开时又会被云端旧条目回灌。

在自己写这个项目之前，我也找过很多开源工具。它们分别解决了跨浏览器同步中的重要部分，但我没有找到一个同时覆盖“明确指定来源和目标、本地备份与回滚、Safari 支持，以及针对云端回灌的实际修复流程”的方案。因此，Bookmark Sync 就是从这个缺口开始写的。

> **核心问题：** 多浏览器在现实中很有必要，但它们的书签存储格式和云同步行为并没有真正互通，无法安全地保持一棵可信书签树。

这个项目不是为了替代浏览器原生云同步，也不是要再做一个托管书签云服务，而是提供一个本地优先的桥接层，用于迁移、可控镜像、备份、校验和针对浏览器差异的修复。

## 开发说明

本项目主要基于 OpenAI Codex 完成开发，Codex 负责主要的编码和集成工作；其他 agent 在定向调研、审查、验证、文档和发布检查等环节提供辅助。项目范围、安全边界、实际浏览器操作和公开发布决策由维护者最终确认。

> [!WARNING]
> 这是镜像工具，不是合并服务。正式同步会替换目标浏览器中已映射的书签树。请先预览同步方向，并保留默认自动备份。

## 解决的难题

- **Edge 云端回灌：** 本地文件替换成功后，Edge Sync 可能在浏览器打开时恢复旧云端条目。`auto` 策略会检测打开后的漂移、按目标记录问题，并为受影响的 Chrome/Edge 配置切换到“浏览器 API 清空、等待云端稳定、恢复来源”的流程。
- **浏览器格式不同：** Chromium 使用 JSON 书签树，Safari 使用根节点和元数据不同的 plist。项目通过可移植书签树映射书签栏、菜单、目录、网址和可支持的同步根，而不是跨浏览器复制原始文件。
- **标识反复变化：** 节点匹配时复用 Chromium ID/GUID 和 Safari UUID，减少无意义的删除重建及云同步噪声。
- **一次性脚本不安全：** 默认逐目标备份、原子写入、重新加载校验；恢复前还会再次备份当前目标，并在校验失败时自动回滚。
- **不同浏览器时序不同：** 通过稳定窗口、历史观测、`doctor` 和 `calibrate` 处理浏览器及云同步延迟，而不是假定一个固定等待时间适用于全部浏览器。

## 核心优势

| 能力 | 说明 |
| --- | --- |
| 手动指定方向 | 任意已检测的受支持浏览器都可作为来源，并同步到一个或多个目标。 |
| 两套同步策略 | 普通或纯本地配置使用快速直写；仅在 Chrome/Edge 确认存在回灌时使用云安全修复。 |
| 数据安全保护 | 默认备份、`0600` 权限、原子替换、写后校验、带回滚备份的恢复。 |
| 本地隐私 | 无遥测、无 API Key、无托管服务，不上传书签；状态文件只保存哈希和时序观测。 |
| Agent 可调用 | 提供可安装、确定性的 CLI 和稳定 JSON 输出，并保留可选 Codex/Hermes Skill 指令；Skill 是适配层，CLI 才是核心。 |
| 易于扩展 | 浏览器检测和格式处理器分离注册，新增 Chromium 浏览器可复用现有格式处理器。 |

## 浏览器支持

| 浏览器 | 直接同步 | 云安全修复 | 验证状态 |
| --- | --- | --- | --- |
| Chrome | 支持 | 支持 | 真实本地配置及隔离逻辑测试 |
| Edge | 支持 | 支持 | 真实本地/云端校准及回灌修复 |
| Safari | 支持 | 仅直接校验 | 真实本地配置 |
| Brave | 支持 | 未校准 | 隔离配置，全部 Chromium 交叉方向 |
| Vivaldi | 支持 | 未校准 | 隔离配置，全部 Chromium 交叉方向 |
| Opera | 支持 | 未校准 | 隔离配置，全部 Chromium 交叉方向 |
| Arc | 不支持 | 不支持 | 使用专有侧边栏归档，并非标准 Chromium 书签库 |
| Firefox | 不支持 | 不支持 | 需要事务化处理 `places.sqlite` |

Brave、Vivaldi、Opera 的账号云同步尚未校准，建议关闭其书签云同步后使用直接模式。

## 快速开始

要求：macOS、Python 3.11 或更高版本；若 macOS 拒绝访问 Safari 书签，需要为终端或应用授予“完全磁盘访问权限”。

```bash
git clone https://github.com/roanpy/browser-bookmark-sync.git
cd browser-bookmark-sync
./sync-bookmarks --list
```

无需克隆仓库，也可以直接安装已发布的 `v0.1.1` wheel：

```bash
python3 -m pip install --user https://github.com/roanpy/browser-bookmark-sync/releases/download/v0.1.1/bookmark_sync-0.1.1-py3-none-any.whl
```

不保留源码目录也可以安装可调用 CLI：

```bash
python3 -m pip install --user .
bookmark-sync --list
```

需要隔离用户环境时可使用 `pipx install .`。项目没有运行时依赖，当前仍仅支持 macOS。

只预览 Chrome 到 Edge 和 Safari，不写入：

```bash
./sync-bookmarks --from chrome --to edge safari --mode preview
```

Agent 和 CI 可以从 stdout 读取 JSON，详细人工日志会写到 stderr：

```bash
bookmark-sync --from chrome --to edge safari --mode preview --json
bookmark-sync --doctor edge --json
bookmark-sync --list-backups --json
```

JSON 协议带有版本号，包含操作、退出码、选定书签库、策略、备份路径、doctor 诊断、结果数量和校验摘要，不包含书签标题或 URL。`--list-backups` 即使在没有可用浏览器书签库时也能工作，并按新到旧返回目标、创建时间、备份年龄、大小和路径。

Codex 可以安装仓库提供的可选 [Bookmark Sync Skill](skills/bookmark-sync/SKILL.md)：

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills/bookmark-sync"
cp skills/bookmark-sync/SKILL.md "${CODEX_HOME:-$HOME/.codex}/skills/bookmark-sync/SKILL.md"
```

Skill 负责提供 Agent 安全边界与调用规则，实际执行入口仍是已安装的 `bookmark-sync` CLI。

严格同步；请先关闭相关浏览器，或明确允许工具自动关闭：

```bash
./sync-bookmarks --from chrome --to edge safari --auto-close
```

若 `auto` 判断需要云安全修复，必须明确允许临时清空目标云端书签：

```bash
./sync-bookmarks --from chrome --to edge --auto-close --allow-cloud-purge
```

恢复备份；恢复前会再次备份目标当前状态：

```bash
./sync-bookmarks --restore-backup ~/Downloads/bookmark-sync-backups/BACKUP_FILE \
  --restore-target edge --auto-close
```

备份默认位于 `~/Downloads/bookmark-sync-backups`，运行状态默认位于 `~/Library/Application Support/Bookmark Sync/state.json`。可使用 `BOOKMARK_SYNC_BACKUP_DIR`、`BOOKMARK_SYNC_DATA_DIR` 覆盖；隔离测试可使用 `BOOKMARK_SYNC_HOME`。

## macOS 应用

```bash
./build-macos-app
open "dist/Bookmark Sync.app"
```

应用会在运行前确认来源、目标、浏览器关闭、备份和可能的云端清空。公开分发二进制仍需要 Developer ID 签名和 Apple 公证；当前仓库发布源码，不提供已公证安装包。

带标签的发布流程见 [RELEASING.md](RELEASING.md)。没有 Apple 凭据时也可以发布源码和 wheel；只有完整配置 Apple 凭据后才会附加签名、公证的 macOS 应用。证书、密码和 Token 不会进入仓库。

## 安全与隐私

- 云端清空必须显式传入 `--allow-cloud-purge`，并禁止与 `--no-backup` 同时使用。
- 云端清空失败时，工具会关闭目标浏览器，并尽力从清空前备份恢复本地书签。
- 相关浏览器关闭后会重新读取来源书签，避免使用关闭前的旧快照。
- 原子替换失败时也会清理可能残留的临时书签文件。
- 备份包含完整书签数据，不要上传到公开仓库或 Issue。
- 状态文件只包含书签库 ID、SHA-256 签名、时序观测和策略历史，不保存书签标题或 URL。

临时 Chrome/Edge 扩展只在云安全修复期间存在，并使用官方 Chromium 书签 API。参考 [Chrome bookmarks API](https://developer.chrome.com/docs/extensions/reference/api/bookmarks) 和 [Microsoft Edge 扩展 API 支持](https://learn.microsoft.com/zh-cn/microsoft-edge/extensions/developer-guide/api-support)。

## 验证

```bash
python3 -m unittest discover -s tests
python3 -m py_compile bookmark_sync.py sync_bookmarks.py sync-bookmarks
python3 -m pip wheel --no-deps . --wheel-dir /tmp/bookmark-sync-wheel
ruff check .
```

自动测试覆盖格式转换、稳定标识复用、策略选择、云端清空授权及回滚、备份恢复、稳定校验、命令封装和真实 JSON CLI 子进程协议。Brave、Vivaldi、Opera 还在隔离配置中测试了全部六个来源/目标方向，包括浏览器重开和逐字节备份恢复。

版本记录见 [CHANGELOG.md](CHANGELOG.md)，当前最新公开版本为 `v0.1.1`。

提交浏览器格式或恢复逻辑变更前，请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [TEST_MATRIX.md](TEST_MATRIX.md)。

## 范围与限制

- 只处理书签；不会修改历史记录、密码、标签页、扩展或其他浏览器设置。
- 不合并并发修改，也不替代浏览器原生云同步。
- Safari 的磁盘格式属于平台内部实现，macOS 更新后可能需要适配。
- Chrome/Edge 云安全模式会先清空选定目标的书签树，再恢复指定来源；预览和备份是必要安全边界。

安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。项目采用 MIT 许可证；浏览器名称和商标归各自所有者，本项目与其无隶属关系。
