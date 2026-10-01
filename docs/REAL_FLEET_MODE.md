# Real Fleet Mode & Antigravity Track Architecture

`/fleet` coordinates two real, synchronous coding-agent executors:

1. Lead: inspect the repository and produce a testable acceptance contract.
2. Builder: edit the workspace and run relevant tests.
3. Lead: inspect the resulting workspace, rerun verification, and return `VERDICT: PASS` or `VERDICT: FAIL`.

PocketFleet never substitutes simulation output for an unavailable executor.
`/sim` is an explicit preview mode and must state that no agent, file change, or test execution occurred.

## Telegram Identity

Use one dedicated PocketFleet bot for inbound commands and outbound status.
Do not reuse a personal agent bot token: Telegram permits only one `getUpdates` consumer per bot, so reuse will make the agent and PocketFleet steal messages from each other.

Agent names in fleet output identify the executor that produced the content.
PocketFleet does not send generated text through an agent's personal bot token.
An independent Codex CLI turn is labeled `OpenAI Codex worker`; it is never presented as a named teammate or as the user's current interactive Codex conversation.

## Antigravity CLI Execution

PocketFleet uses the official `agy --print` interface and waits for its process to return the actual model response. Configure `POCKETFLEET_ANTIGRAVITY_CLI` in `.env` only when `agy` is not already on `PATH`.

The product does not inject tasks into a user's interactive Antigravity IDE track.
`agentapi send-message` is only a delivery acknowledgement and is never accepted as completed work.

---

## 附录：Antigravity 轨道物理隔离与快照续轨实战指南 (PF-02)

### 1. 硬事实：IDE 与 CLI 轨道的物理隔离
在实际工程验证中，Google Antigravity 的 IDE 运行态与官方命令行 CLI (`agy`) 具备以下物理特性：
- **存储物理隔离**：
  - IDE 会话存储于 `%USERPROFILE%\.gemini\antigravity-ide\conversations\<UUID>.db` 与对应 `brain\<UUID>\` 目录；
  - CLI 会话存储于 `%USERPROFILE%\.gemini\antigravity-cli\conversations\<UUID>.db` 与对应 `brain\<UUID>\` 目录。
- **命令不互通**：
  - 实测直接在终端执行 `agy --conversation <IDE-UUID>` 会报错 `Conversation not found`，并自动在 CLI 目录下建立全新的空会话，导致上下文断裂。
  - 严禁通过直接复制 SQLite 数据库或 brain 目录冒充导入。
- **官方唯一合宪迁轨路径**：
  ```bash
  agy
  # 进入交互界面后执行:
  /resume
  # 按 Tab 键切换到 Antigravity 列表，选中目标 IDE 轨，按回车确认 Import
  ```
  导入成功后，CLI 将生成一个**全新的快照 CLI 轨 UUID**。

### 2. 快照续轨机制 (Snapshot Continuity)
官方交互 Import 的本质是**时间点快照克隆（Point-in-Time Snapshot Clone）**：
- **快照生成**：IDE 当前落盘对话与思考被克隆至 CLI 轨；
- **独立持久化**：新生成的 CLI 轨拥有独立的 SQLite 数据库与持久化 brain 目录；
- **施工续轨绑定**：在 `.env` 中配置 `POCKETFLEET_ANTIGRAVITY_CONVERSATION_ID=<新CLI轨UUID>`，`agy` 执行时自动追加 `--conversation <新CLI轨UUID>`。

### 3. 再次分叉与重新导入 (Forking Lifecycle)
- 导入完成后，IDE 轨道与 CLI 轨道在物理上解耦；
- 开发者在 IDE 窗口中继续的对话只留在 IDE 轨；PocketFleet 通过 Telegram / 控制台驱动 CLI 完成的代码施工与对话也不反写回 IDE 窗口；
- **重新导入标准作业 SOP**：
  1. 在控制面板点击【Stop All Services】停止 Telegram Daemon（避免 409 冲突）；
  2. 打开【🧭 Antigravity 轨道】，点击【🚀 开始官方导入】；
  3. 在新终端中输入 `/resume`，按 Tab，选中最新的 IDE 轨道回车导入；
  4. 关闭终端，在控制面板中点击【🔎 检测导入结果】；
  5. 差集算法确权后点击【🔗 绑定为施工轨】；
  6. 启动 Telegram 服务，战队恢复运转。

### 4. 防御型工程师纪律红线
1. **零虚假实时并轨宣传**：清楚告知用户这是“官方导入后的快照续轨”，绝非实时双向内存并轨；
2. **换轨必停 Daemon**：Telegram 长轮询存活时严禁换轨，防止 409 Conflict 异常；
3. **禁止猜最新**：导入检测采用严格前置快照差集比对，新增 0 条或多于 1 条时一律 Fail-Loud 报错；
4. **凭据安全**：`.env` 仅写入会话 UUID，绝不写入任何真实 Token 或密码明文；`.gitignore` 必须严密阻断 `*.key`, `*.pem`, `*.token`, `*.p12`, `*.pfx`, `id_rsa*`。

### 5. 返航回流连续性闭环（Return Continuity Protocol）
用户在外勤（手机端通过 CLI 轨）下达施工任务时，返航回到电脑前与 IDE 协同并非发生在“完工时刻”，而是在 **CLI 开工时刻（Commencement）** 即建立前置握手：
- **开工即握手（Launch-time Handshake）**：由于外勤任务往往是长程运行，CLI 无法预知完工时间，甚至任务尚未完工时用户就已回到电脑桌前。因此宿主守护进程在 CLI 启动施工的第一时间，立即通过原生 `agentapi send-message` 向活动 IDE 会话推送开工交接公文。
- **三道防线支撑平滑接管**：
  1. **第一道：底层物理黑匣子**：CLI 轨施工的全部过程被官方自动写入 `~/.gemini/antigravity-cli/brain/<UUID>/.system_generated/logs/transcript.jsonl`，落盘即存，物理不可磨灭。
  2. **第二道：工作区交接备忘录**：宿主自动在工作区根目录写入 `.fleet_handover.md`，记录开工时间戳、外勤会话 UUID、当前施工指令摘要与实时黑匣子路径。
  3. **第三道（核心）：寻址公文前置送达**：IDE 窗口内的 AI 裁决者在开工时刻即接收到该公文并保持就绪。无论用户中途何时返回 PC，在 IDE 提问外勤进展或要求接手，AI 均可直接调用 `view_file` 查阅外勤黑匣子实时日志，无缝读入外勤上下文，实现免重启、免重建轨的就地认知接关。

### 6. SQLite 事务级快照保障 (Zero WAL Tearing)
克隆 IDE 轨至 CLI 域时：
- 采用 SQLite 官方 Backup API（`sqlite3.connect().backup()`），以只读 URI（`file:<path>?mode=ro`）连接源库；
- 保证绝不引起任何活跃 IDE 进程的锁冲突；
- 自动将活动 WAL 页刷入目标单文件 DB，彻底杜绝 WAL 事务撕裂与数据库损坏；
- Brain 目录采用非破坏性增量拷贝（`shutil.copytree(dirs_exist_ok=True)`），彻底废除破坏性 `rmtree`。

### 7. 商业软件大占有率铁律·“极致降阻”三法则 (Command Axiom)
> “你会的代码，别的AI也会；但是这一套从真实用户阻力出发的‘无感与接管’哲学，程序员往往不会、代码AI多半也不会。这就是做软件能拿下大市场占有率的核心原因。”

1. **代理零配置**：自动识别 Windows 系统代理与常见代理端口（7890/10808），让 Telegram 无论在什么网络环境下一键直通，绝不让用户手动配环境变量或改系统设置；
2. **多开自愈**：启动时单实例互斥锁守护，遇到旧后台进程残留或端口占用时自动平滑接管，绝不让小白用户去翻任务管理器；
3. **托盘静默**：关闭窗口自动最小化至系统托盘，不留命令行黑框，开机自启无感守护，彻底消除极客黑框的认知心理负担；
4. **全内置零门槛交付**：运行时环境（Python 及所有依赖）全量打包入独立二进制安装包，用户电脑零 Python 也能双击秒开，彻底切断“部署前先装环境”的转嫁病。
