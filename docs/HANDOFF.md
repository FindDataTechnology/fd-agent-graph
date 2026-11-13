# 交接：agent 图工程化 + 编码班底

2026-11-08。这份文档只写**盘上的事实与判据**，不写推导过程。
写它的原因：下个会话不会继承对话，而「已验证 / 未验证 / 待人工」三者混在一起最容易误判。

> **这是 2026-11-08 的快照，文中数字（如「217 单测」）是当时的读数，不随代码更新。**
> 当前口径以 `README.md` 为准——2026-11-14：385 单测通过 / 5 skip、12 个子命令、
> `scripts/check_docs.py` 与 `scripts/check_copies.py` 双绿。历史数字不改写，是因为
> 这份文档的价值就在「当时看到的是什么」。

---

## 一、一句话说清这是什么

把「多个 agent 协作写代码」当成**一张显式的图**来定义、检查、度量，而不是写在 prompt 散文里。
工具不执行 agent；它判形状、判契约、判合并点会不会丢信息。

`openspec/specs/` 里 8 份规格：`agent-graph-ir` / `-checking` / `-topology-analysis` / `-gate` /
`agent-binding-manifest` / `-validation` / `code-graph-service` / `-task-anchoring`。

---

## 二、交付物与**各自的验证判据**

| 交付物 | 位置 | 验证到什么程度 | 判据 |
|---|---|---|---|
| 图检查器 | `fd-agent-graph/` | 199 单测绿；真实图实证；四路审计 | `unittest discover` rc 0；`scripts/verify_real.py` |
| 编码班底 roster | `roster/finddata-coding.yaml` | 5 角色 / 9 实例，四层对账通过 | `bind --check-host` rc 0 |
| agent 定义生成器 | `agentgen.py` + `export` | 生成 ≠ 安装，逐字节漂移守卫 | `AgentGenTests.test_shipped_plugin_matches_generator` |
| 谦面包 | pack `w11_2jocz4UiMeGsdRj0nA` v1 | **已发布 public**，中英双轨回读 | `GET /api/packs/<id>?locale=zh|en` |
| 公开仓插件 | `fd-vertical-packs` main `7d16fcc` | fresh clone 复核；官方 preflight 0 error | `preflightPlugin()` + 7 项市场解析核对 |
| 本地插件 | `plugins/finddata-coding-crew/` | **已装入宿主**：缓存 + `plugin.json` + `known_marketplaces.json` + `enabledPlugins`，`bind --check-host` 5 告警 → 0 | `bind --check-host` rc 0；`accept.py` → installed definitions 5 |
| 派发钩子 | `scripts/dispatch_hook.py` | **已实测生效**：探针 prompt 含 `implementer` → 日志新增 `pass crew dispatch, roster clean` | `build/dispatch-hook.log` 有无新增行 |
| 谦面包是否可装 | `POST /api/packs/<id>/subscribe` | **200 + 完整 manifest**（含 skill 正文）；网页按钮只提示「在壹座内完成」；`/install` 路由不存在 | 登录态 fetch 返回 200 且 manifest 完整 |
| 终局验收 | `scripts/accept.py` | 4 个真实 change 出回执 | `build/<change>.receipt.json` |
| 副本一致性 | `scripts/check_copies.py` | 技能 3 份逐字节一致 **+ manifest 描述与正源 frontmatter 一致** + 守卫的守卫 | rc 0 / 篡改必红 |
| 文档可执行性 | `scripts/check_docs.py` | 10 个子命令名存在、6 条文档命令按声明退出码跑通、路径存在、单测数一致 | rc 0 / 编个假命令必红 |
| 审计班底 | `build/audit.yaml` + `roster/audit.yaml` | 4 路只读侦察已实跑一轮 | `metrics --roster roster/audit.yaml` → width 4 |
| **真实班底回合** | `/tmp/crew-drill/` | **planner → 2×implementer 并行 → merger → verifier 全跑通**；合并点四个名字全带；我方独立复跑 `rc=0` | 见下「三·七」 |
| 任务→文件归属 | `fd_agent_graph/planner.py` + `plan` 子命令 | 三种诚实结果；实测一个 change 归属 6 个真实文件（合并图下为 0） | `plan --change-tasks … --repos …` |
| 归属带进骨架 | `init --plan-repos` | 归属写在分支首个节点的 description，不伪造端口；占位符仍在，门照样红 | `init <tasks> --plan-repos …` |
| 代码图索引 | `attic/fd-code-graph/build/shards` | 已重建（128MB，277,995 节点） | `python3 -m fd_code_graph.cli stale` |

---

## 三、只剩**一件**必须人工的事（已尝试，做不到）

### 桌面端本会话内看不到新角色

新装/新注册的 agent 类型在**当前桌面会话**里不可见（类型表在会话启动时绑定），
`Agent type 'planner' not found` 就是这个。**不是没装上**：换一个全新 CLI 进程立刻能看到五个角色
（见上一节）。所以这条其实也不需要人工动作——下次开会话自然就有。

### 已解决（原文档写「必须人工」，其实一直有正路）

1. **装插件——有官方命令，别手工写缓存。** 宿主自带 CLI：

   ```
   node /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs plugins list
   node ... plugins marketplace list
   node ... plugins install finddata-coding-crew@finddata-official
   ```

   本次先手工把 `.zcode-plugin/plugin.json`、`config.json` 的 `enabledPlugins`、
   `known_marketplaces.json`、`marketplaces/<id>/marketplace.json`（带 `cachePath` + `source: filesystem`）
   都写对了——`bind --check-host` 甚至归零了——但 `plugins list` 里**根本没有这个插件**，
   而且装完后宿主重启 4 次都没加载它。一句 `plugins install` 就装上了。
   **判据是 `plugins list` 列出该插件并标 `[enabled]`**，不是文件在不在，也不是 `bind --check-host` 的告警数
   （`bind` 读的是目录，宿主读的是注册表——两者可以同时"正确"却互不相干）。

   验证角色真的能派发，用一个**新进程**即可，不必重启桌面端：

   ```
   node .../zcode.cjs -p "List the subagent types you can dispatch with the Agent tool." --cwd /tmp --mode plan
   → Explore, explorer, finddata-coding-crew:{planner,explorer,implementer,merger,verifier},
     general-purpose, implementer, merger, planner, verifier
   ```

2. **钩子生效**——已用含 `implementer` 的探针实测（见上表）。

3. **谦面包能不能装**——`POST /api/packs/<id>/subscribe` 200 并返回完整 manifest；网页的
   「订阅并安装」只提示「在壹座内完成」，`/api/packs/<id>/install` 这个路由不存在。

---

## 三·五、四路审计（2026-11-08）改掉了什么

用**现有** agent 类型按班底形状跑了一轮真实审计：4 路只读侦察 → 合并点消费全部约束 → 验收。
**插件没装也能跑**——模板约束的是工作形状，不是 agent 类型。形状：width 4 / cp 5 / ceiling 1.6。

它抓到的第一条 high 就是「我用假信号宣布钩子上线」：钩子单测会真跑脚本，而日志路径硬编码，
于是「日志有没有新增行」无法区分「真实派发」与「跑过单测」。已修。

**修掉的沉默缺陷（原则：查不了 ≠ 没问题）**：

- 端口段非映射 / `constraint` 非布尔 → `graph.node.malformed-ports`（error）
- 边携带了目标未声明的输入 → `graph.edge.value-unconsumed`（warning）
- `--roster` 展开丢 `unresolved`/`malformed` → 保留（曾让 `UNRESOLVED_SUBGRAPH` 蒸发）
- 容量拒绝 `blocking=()` → 产出 `graph.dispatch.above-width`
- 坏输入 traceback + 退 1 → 退 2 + 一句话；`--concurrency<=0` → 退 2
- 宿主定义读不出 → `binding.host.unreadable`（error）；不限工具 → `tools-unconstrained`（warning）
- 显式坏 `--skills-root` → `skills-root-missing`（error；默认根不存在仍只是 warning）
- `bound: -1` 抑制无界环 → 只有 `bound > 0` 才算有界
- YAML 重复键 last-wins → 报错带行号；多文档 `---` → 报错；未闭合 flow → 报错带行号
- `metrics`/`baseline` 静默丢 findings → 带计数 + stderr 提示
- `--json` 错误负载字段不全 → 统一 `{target, exit_code, error, summary, findings}`
- 子图按 CWD 解析 → 按定义文件目录
- pack skill description 与 SKILL.md frontmatter 不一致 → 已对齐，`check_copies` 现在也比 frontmatter
- `build/agents/` 无人守卫的第 5 份副本 → 加守卫
- 文档里的 `fg` 命令不存在 → 全部改为 `python3 -m fd_agent_graph.cli`

**仍未修（有意保留，理由在内）**：`--json` 只在部分命令上接受（其余本就总输出 JSON）；
roster 的 `template:` 是相对路径，**清单被移走就断**——钩子单测的夹具因此写绝对路径。

## 三·六、`plan`：把 `file_ownership` 从 TODO 变成实据

这是整套东西里唯一长期留给人手填的约束。现在：

```
plan --change-tasks <change>/tasks.md --repos fd-wire,paas --workers 4
  → 4 片，文件数 497 / 497 / 495 / 697，criteria 12 条（从「验证=…」抽出）
```

**通过 CLI 借用 `attic/fd-code-graph`，不 import**——两个包互不依赖，任何一个可替换。
**算不出来就说不出来**：索引缺失或工作区读不出时 `index_available: false`、`files` 为空、
`note` 写明原因。**绝不编造文件归属**——编出来的会被合并点当真消费，那比没有更坏。

**索引来源很关键**：`fd-code-graph` 默认从工作区**合并图**重建，而 `graphify update <repo>`
只刷新 `*/graphify-out/graph.json`——合并图不会跟着变，于是新文件永远进不了索引。
已加 `rebuild --per-repo`：从 54 个仓各自的图重建（325,099 节点 vs 合并图 286,653）。

实测差别：`wire-alert-delivery` 用合并图 → 归属 0；用 per-repo 索引 → 归属 6 个真实文件
（`alerts_core/delivery.py`、三个 alert workflow、两个 scripts）。**合并图过期不是小事，是让整个 plan 归零。**

**第一版是错的，实测出来的**：它把整仓分区当归属——一个真实 change 分到 **2,186** 个文件，
而任务只点名 **14** 个（156 倍）。那不是约束。

现在三种诚实结果：解析到的文件才认领（个位数）；一个都没解析到就**不认领**并说明分区只是调度单位；
没有索引就说没有。**绝不拿分区冒充归属**，因为合并点会真的消费它。

踩到的四个坑（都已修并写进测试）：`--root` 会连带移动 build 目录，必须显式 `--build` 指回；
工作区根不能数 `parents[N]`（第 3 层是 `openspec` 不是根）；`lstrip("./")` 会把 `.github/…` 削成
`github/…`（要按前缀剥，不能按字符集）；裸文件名（`manifest.js`）匹配多个包，必须拒绝而不是猜。

## 三·七、真实班底回合（2026-11-08，唯一一次端到端）

不是模拟、不是「用现有类型假装」：在 `/tmp/crew-drill/` 用一个真实 CLI 进程
（`node .../glm/zcode.cjs -p <prompt> --cwd /tmp/crew-drill --mode yolo`）跑完一整轮。

任务刻意设计成**合并点必然丢东西就会当场露馅**的形状：两个互不相干的模块
（`stats.py`: mean/median；`fmt.py`: format_number/format_pct），合并点 `pkg/__init__.py`
必须同时 re-export **四个**名字；测试只有一行 `from pkg import` 四处名字——少一个就红。

实测结果：

| 阶段 | 子代理 | 事实 |
|---|---|---|
| 规划 | `planner` | 切两片、两片文件不相交、口径逐字引用 SPEC |
| 并行实现 | `implementer` ×2（**同一条消息里发出**） | A: stats.py → mean/median；B: fmt.py → format_number/format_pct |
| 合并 | `merger` | `__init__.py` 四个名字全在，`__all__` 也列全 → **无约束丢失** |
| 验收 | `verifier` | 写 5 个用例并跑 `unittest discover` |

**我方独立复跑**（不采信验收者自述）：`python3 -m unittest discover -s tests -t .` → **rc=0**，
`Ran 5 tests`，`from pkg import mean, median, format_number, format_pct` 一行成功，
`2.0 2.0 1.23 12.34%`，空列表抛 `empty`。

**派发钩子同轮生效**：做完一次受控增量（先记行数 → 新进程派发一次 → 再记）→ **恰好 +1 行**
`pass crew dispatch, roster clean`。之前的「日志有新增」不能证明什么（单测也会写），
带 before/after 计数的增量才行。

验收者自己报了第一次 `rc=1`（Python 3.14 下 `unittest discover` 要求 `tests/` 可导入），
补 `tests/__init__.py` 后重跑 rc=0——**这个诚实自报比绿本身更有价值**，说明角色提示词里的
「报真实 rc，不要粉饰」是有效的。

## 三·八、第二回合：windows-admin 上用这套东西真建了一个项目（2026-11-08）

目标机 `100.64.0.5`（用户 `admin`，密钥 `~/.ssh/windows-admin-control`；**无 Python**，
Node v24.20.0，Git Bash，无 Docker）。所以项目做成**零依赖 Node**——不往数据机上装任何东西。

产物两处：工作区 `fd-win-health/`（源，便于版本管理）、目标机 `D:\fd-win-health\`（运行副本）。
change `add-win-health-watchdog` 13/13 全勾，`openspec validate --strict` 绿。

**图先行**：`examples/win-health.yaml` + `roster/win-health.yaml`。
第一次 `check` 就抓到一条真丢约束：`verify` 要判「三个退出码是否写进 --help」，
却没承接 `exit_contract` 端口。补端口 + 补边之后 0 warning。
形状 `width 2 / critical_path 5 / ceiling 1.2`，`may-dispatch --concurrency 2` → permitted（带收益警告）。

**真跑结果与两个真缺陷**：

| 阶段 | 事实 |
|---|---|
| planner | 两片文件集互不相交，契约逐字复述 |
| implementer ×2 | **同一秒起跑**（18:29:29），并行是真的 |
| merger | 由主 agent 承接（三路导出名逐字核对）：`probeDisks/probePort/probeProcess/buildReport/exitCodeFor` |
| verifier | **被上游 502 打断**（`Upstream returned an empty completion without usage; no fallback account was available`），角色退出、没写出测试 → 验收由主 agent 接手 |

1. **一路卡住会静默拖住合并点**：branch B 18:29 起跑、18:44 才落文件、之后 17 分钟不退出，
   合并点一直在等。图能算出「宽 2」，但**没有任何租约/超时机制发现某一路不再前进**。
   这轮的真因是上游 502 反复重试（不是图的错），但结论一样：形状算得再准，也需要**运行时租约**。
2. **契约写不死，两路就会各自"自洽"**：`disk` 用了 `statfsImpl`（我写的是"statfs"）、
   单盘读失败选择抛错而不是给 error 项、`process` 压根没有注入点。三处都是
   「实现自洽、测试也对，但彼此对不上」。教训：**注入点的名字和失败形状必须写进契约**，
   否则合并点是靠运气对齐的。

**真机验收（D:\fd-win-health）**：`node --test` **16/16 全绿**（Mac 与 Windows 两边都跑）；
三档都用真命令验过——`ok=0`、`degraded=1`（D 盘 12.17% 余量越软阈值）、
`critical=2`（59999 端口关闭，错误信息 `ECONNREFUSED 127.0.0.1:59999`）。
磁盘与 `wmic logicaldisk` 对账、PID 与 `tasklist` 对账均一致。

**顺带发现的真话**：D 盘只剩 **65.4G / 536.9G = 12.2%**，C 盘 25.0% 压线——
这台数据机没有人在盯盘，这个看护器不是练习。

**本轮在自己身上也修了四处**（都是实弹逼出来的）：

- `check` 不收 `--roster`，而 `metrics`/`may-dispatch` 都收——CI 会调的那条命令恰好只回答
  了「模板宽 1」这个没人问的问题。已补，并加了钉死它的用例。
- 两条用例把 `../openspec/changes/<某个活 change>/tasks.md` 写死。那个 change 被**另一个
  并发会话归档**后，本包什么都没改却红了两条。已改为自建临时夹具：**测试不许拿仓库当人质**。
- `probePort` 把拒绝连接报成 `"AggregateError"`——等于没有诊断。已解包内层错误，
  现在给 `ECONNREFUSED connect ECONNREFUSED 127.0.0.1:59999`。
- `probeProcess` 完全不可注入，只能上真 Windows 才测得了。已补 `platform`/`run`。

全量：**217 单测 OK**（skip 4 条网络用例）；`check --roster` / `metrics` / `may-dispatch`
三条在 `examples/win-health.yaml` 上都是 0 error 0 warning。

## 三·九、第三回合：按实测调队 + 让班底继续写 v0.2（2026-11-08 晚）

**调整依据全是上一轮测到的，不是猜的**：

| 观察到的事实 | 调整 |
|---|---|
| branch B 扛 `cli.js`（338 行，是其余文件 2.6 倍），比 A 晚 8 分钟收口 | **2 路 → 3 路**，把最长文件单独切一路（`examples/win-health-v2.yaml`，width 3，ceiling 1.2 → **1.4**） |
| 三路文件都写完，角色 17 分钟不退出，合并点干等 | **完成标记协议**：每路必须落 `.crew/<branch>.json`（branch/files/exports/wallclockSeconds），`branch_status` 升为 constraint，合并点与验收者都必须承接 |
| 缺一路信号时合并点无限等 | **合并点改为不阻塞**：缺哪路写进 `dropped` 并继续。无限等不如可见的丢弃 |
| 上一轮验收者能拿到的只有"文件存在" | 合并点只认标记里的 `exports`，并逐一核对文件存在；本次它自己写出了三份标记清单与 `dropped: none` |

**这一轮新增的两类失败（都记下来，下轮要用）**：

1. **写完被宿主判死**：合并者文件写对了、十名导出齐全，但角色状态是 `failed`——
   `Subagent was inactive for 600000ms`。宿主有 **600 秒静默阀**，我改不了；
   所以协议要求"合并者的活要短到能在阀内收口"。
2. **503 打断编排**：编排进程被 `Service temporarily unavailable` 反复打，验收者没起来。
   与上一轮 502 同类：**基础设施不稳时，编排层必须能接手**，不能让它变成"任务失败"。

**v0.2 交付**（真机 `D:\fd-win-health`）：

- `src/config.js` → `loadConfig` / `DEFAULT_CONFIG`（未知键与类型错误都点名，读失败带路径）
- `src/store.js` → `appendReport`（JSONL + 按字节轮转 `.1` + `maxLines` 截断）/ `readRecent` / `summarize`
- `src/cli.js` → `--config` / `--report` / `--max-lines`，`--thresholds` 就近覆盖配置文件
- 合并点十名导出：五个旧探针 + 五个新能力

**真机实证**：`node --test` **33/33**（Mac 与 Windows 两侧）；真配置文件 `winadmin.json`
跑出 `degraded` 且退出码 1，D 盘已降到 **12.08%**；`--report` 落 JSONL；
连跑 5 轮 `--max-lines 3` 后文件恰好 3 行且保留最新——三项新功能都不是纸面功能。

## 三·十、第四回合：把"验收端口"变成可执行的闸门（2026-11-09）

前三回合我一直在**口头上**要求三件事，而它们在真机上各出过事故：

| 口头要求 | 真机上出过的事 |
|---|---|
| "跑一次 CLI 看看" | 一个工具**退出 0 一字不吐**（缺入口守卫）；另一个**挂死 90 秒**（无界递归遍历） |
| "测试要能跨平台" | 三次平台差异全是测试里写死 POSIX 绝对路径，macOS 绿、Windows 红 |
| "标记要有 exports" | 两路把 `exports` 写成整行函数签名；还有一路**报 completed 却零文件** |

所以我加了三个端口（`cli_seconds` / `cli_output_lines` / `platform_paths_ok`）——但**端口没人执行就是装饰**。

### `scripts/route_gate.py`：端口执行器

```
python3 fd-agent-graph/scripts/route_gate.py --project fd-portmap --min-tests 60     --route snapshot --route allowfile --route summary     --cli-args "--netstat <fixture> --summary"
```

它做四件事，全部靠**跑**而不是靠读：标记存在且 `exports` 是裸标识符并且真的在 `src/*.js` 里存在；
`node --test` 的 `tests` 数不低于门槛且 `fail 0`；真跑一次 CLI 并量**秒数**与**输出行数**；
扫测试文件里的 POSIX 绝对路径字面量。

**三种退出码是刻意的**：`0` 通过、`1` 项目有问题、`2` **门自己量不了**（例如 PATH 里没有 node）。
这个区分是被自己的 bug 逼出来的：第一次接进 `accept.py` 时，最小 env 里没有 `/opt/homebrew/bin`，
四个项目全报 BLOCKED——**门找不到工具，被误报成了项目不合格**。把"量不了"和"不合格"混成一个红，
是这一类工具最容易犯的错。

### 它立刻抓到的两件事（都不是我发现的）

1. `.crew/history.json` **不存在**——那一"路"报 completed 却零文件，从没留下凭据，
   而我一直靠记忆知道这件事。
2. `fd-backup-age` 测试里 **16 处 POSIX 绝对路径字面量**。它们在 Windows 上当时能过，
   是因为 Git Bash 的 `tmpdir()` 恰好也返回 POSIX 风格路径——**那不是可移植，是运气**。

按门点名的清单起队修，验收也交给门：16 → 0，50 测试仍全绿，`verdict: PASS`。

### 目标机上跑不了这道门（事实，不是缺陷）

windows-admin **没有 Python**。所以真机侧的判据是 `node --test`（现在 50 / 68 / 29 全绿），
闸门本身在 Mac 上跑。**别把 `route_gate.py` 搬过去白费功夫。**

`accept.py` 的守卫段现在会为 `fd-portmap` / `fd-backup-age` / `fd-disk-trend` / `fd-win-health`
各跑一次这道门，所以"回执是绿的"意味着有一个程序真的量过，而不是某个角色说它绿。

### 门自己的三条硬教训（都是它自己红出来的）

1. **计数不等于有测试**。实测：空 `test/` 目录 → `tests 0`；**一个有文件但零 `test()` 的文件 → `tests 1`**。
   所以"数 ≥ N"这道判据可以被一个只有注释的文件骗过。门现在另查测试文件里
   `test(` / `it(` 的真实调用次数。
2. **node 的发现规则比"看 test/ 目录"宽**。它也会捡 `src/*.test.js`，所以只清空 `test/`
   仍然会报 `tests 1`。门现在按 node 的规则找文件，而不是按我假设的布局。
3. **锁必须记长命的持有者**。第一版记的是 `crew_lock.py` 自己的 pid，而它当场退出——
   于是每个锁"一上就过期"，第二次 acquire 反而成功。改成 `--pid`（默认父进程），
   并在持锁者已死时明确警告。

**判据自己要能红**：`tests/test_route_gate.py` 有 14 条，其中 12 条是"必须红"的用例
（空测试目录、只有注释的测试文件、签名式 exports、静默 CLI、POSIX 字面量、活锁、死 pid 的stale 锁……），
2 条是"必须绿"。一个从没红过的门和没有门是一样的。

### 参数要验到"真的改变结果"

`--min-points` 写了文档不等于接上了。实测同一份两点数据：`--min-points 5` → `verdict: unknown`，
默认 2 → `warning`。`--horizon 2000` → EXIT=1，默认 30 → EXIT=0。这类"参数存在但被忽略"
的缺陷，只有拿同一输入换参数跑两次才看得出来。

## 三·十一、第五回合：白名单要认"谁"，不是只认"几号"（2026-11-09）

真机 `--summary` 出 27 组：`System` 一个进程占 13 个监听，`svchost.exe` 9 个，`WorkBuddy.exe` 4 个。
由此看出两个盲区，都是"端口号被当成了身份"：

1. **白名单只比对端口号**：`--allowed 9000` 等于"凡 9000 一律放行"，谁抢了 9000 都合规。
2. **`--diff` 的同一性是 proto+addr+port**：同一端口换了进程，`unchanged` —— 看不见。

### 改法（每条都有可执行判据）

- 白名单支持 `9000/rustfs.exe` 绑定。四种结论**必须分开**，因为它们对应不同处置：
  `port allowed` / `port and image match` / `image unknown, binding unverified` / `image mismatch: expected X, found Y`。
  **查不到进程不算合规**——与 process 探针同一条口径：不可观测 ≠ 没问题。
- `diffListeners` 新增 `changed` 桶（同三元组、不同 image）。`summarizeDiff` 末尾 `!n` **只在 n>0 时出现**
  （与"`!0` 会训练人忽略这个字段"同理）。`unchanged` 的含义收窄为"三元组与 image 都相同"。
- CLI 里三处判定（人读列、`--json` 的 `unexpected`、退出码）**统一走 `isAllowed`**。
  第一版只改了打印那一行，于是 `--json` 仍按旧规则说"没事"——同一个问题算两遍就会有两种答案。

### 语义变更带来的连带修正（都记在断言旁边）

`unchanged`→`changed` 的切换让 6 条既有断言失配，它们全都写着"image drift is NOT a change"。
**改断言而不是改实现**，并在每处注明为什么语义变了。`BEFORE/AFTER` 夹具里 5432 的 image 从
`sshd.exe` 改成 `null` 的那一行也一并改回——一个"看不见进程"的样例会把它从 `unchanged` 挤到 `changed`，
让分区断言自相矛盾。

### 团队本身：交接率第一次成了数字

`scripts/crew_ledger.py` 记账，`report` 算率。本轮实测 26 个角色、9 次接手。
**头号原因是上游空回包**（3 次里有 2 次整轮死掉、零文件）。这不改变"该怎么派活"，但改变
"该不该一次派五个"——先修基础设施。

## 三·十二、第六回合：派活之前先量网关，白名单草案交给评审（2026-11-09）

### 并发宽度不该是常数

账本里同一个事实出现了六次：**多路编排一遇上游 502 就整轮死**，不是死一路，是死整轮
（两次里所有路由都为零文件，最后都是我手写）。所以"派几路"应当取决于网关此刻能不能扛。

`scripts/provider_probe.py` 用**同一路径**（bundled CLI + 一个词的 prompt）打 N 次，
把失败率折成宽度：

    3/3 ok   -> 按计划宽度铺开
    2/3 ok   -> 砍半
    <=1/3 ok -> 串行（一次一路）

退出码就是决策（0 健康 / 1 降级 / 2 不可用），shell 不用解析文字。

**本轮实测：2/3 ok，中位 17.8s，一次 90s 超时 → 宽 2，实际只派 1 路。**
这也是本轮没有起多路班底的原因，不是偷懒，是量出来的。

### 白名单草案：把判断交回给人

真机 `--summary` 报 56 个白名单外监听。**我没有替用户决定哪些该允许**——生成了
`--propose-allowlist <path>` 的**草案**：45 个端口全部按当前进程写成 `端口/映像` 绑定，
第一行明写 `# DRAFT … REVIEW BEFORE USE`，进程未知或同端口多主的**注释掉**并要求人工开。

两处刻意的安全属性：从现状生成的名单会默认祝福"现在正在跑的一切"，所以必须标草案；
未知持有者永远不能因为疏忽而变成策略。

真机结果：**45 个端口 / 45 个绑定 / 0 个未知**（"56" 是 IPv4+IPv6 各算一次的去重前计数）。
`8475/baidunetdiskhost.exe`、`7071/aiassistant.exe`、`3246/ElinkUsbIpc.exe` 这些因此有了名字。

### 又一处"默认值与自己写的判据不一致"

`args.propose` 默认 `undefined`，而我的守卫写的是 `!== null` —— 于是每个不带该参数的调用
都去写 `undefined` 路径，`--allow-file` 的正常路径被连带打断（6 条测试红）。
**默认值必须与判据同一个形状**，否则守卫看起来在，实际放行了一半。

## 三·十三、第七回合：运行生命周期（2026-11-09）

### 一轮审计的结论：九个运行机制里只有一个生效

对 `fd_agent_graph/` 逐项核（`file:line` 见 `openspec/changes/agent-run-ledger/design.md`）：
任务状态机、完成判据、持久化、租约/心跳、证据校验 —— **全部没有**；
checkpoint / 幂等 / 介入 —— **半有**（有字段有静态检查，运行时不用）；
**只有 `bound`（重试预算）被运行时完整消费**。

更根本的是 `fd_agent_graph/__init__.py:4` 那句："Nodes are never executed except by the
reference executor used in tests." 真实运行从不经过执行器，所以 IR 里的
`checkpoint`/`bound`/`idempotent` 在真实运行里是装饰。

规格侧同样空白：六份 agent 规格全部止于静态形状，`run`/`ledger`/`lease`/`resume` 零命中。

### 我自己的门，六处名不副实（全部已修）

| # | 位置 | 问题 | 修法 |
|---|---|---|---|
| 1 | `route_gate.py:346` vs `:347` | `should_run_cli` 在 `cliArgs` 回填前求值 → 四个项目的 CLI 检查**从未执行** | 求值移到回填之后；补"参数来自 expect.json"的用例 |
| 2 | `route_gate.py:270` | 契约缺失 `return []` → 无契约即通过 | 新增 `contract: required`，缺失即失败 |
| 3 | `tests/test_route_gate.py` | 契约检查**零用例** | 补 5 条"必须红" |
| 4 | `accept.py:187-197` | 不传 `--route` → 标记检查从不执行 | 按**内容**（有 `branch`）自动发现 |
| 5 | `expect.json` | `cliMaxSeconds`/`allowPosixTestPaths` 死键 | 实现前者；后者显式拒绝并写明理由 |
| 6 | `crew_ledger`/`provider_probe`/`DONE.json`/`verify_real`/`compare_templates` | 无消费者或恒 exit 0 | 前两者接入 accept 回执；其余标注为生成器 |

修的过程中又抓到三个自造缺陷：**假验证**（"还原旧 bug"时新求值行仍在后面执行，测试照样绿）、
`mcpServers` 输出成 `McpBinding(server='graphify', tool='')`（宿主解析必错）、
`skills_declared` 是布尔却当成列表去 join。

### 宿主能力的一句错话，代价很具体

`agentgen.py` 曾断言 "skills / mcp / memory cannot be expressed in an agent file"，
只输出 5 个字段。核查 `zcode.cjs` 的 `parseAgentFrontmatter`：它读 `tools`、
`disallowedTools`、`skills`、`background`、`injectAgentsMd`、`maxTurns`、`memory`
（含 `agent_invalid_memory_scope` 校验）、`permissionMode`、`mcpServers`。
**这六项都可表达**。代价是 roster 里声明的 skills/MCP/memory 从未生效，而
`bind --check-host` 只对账三项，看不到缺口——两处"看起来对"互相掩护。
已更正：生成器现在输出六项，`bind --check-host` 从 **5 warning → 0**。

### 新增 `fd-agent-run`：台账 + 下一步指令

不做服务、不做队列。因为执行者是宿主 agent，无状态可重启，所以运行时只需回答"下一步做什么"。

```
start / next / record / commit / status / resume
```

三条不变量：**I1** 无证据不产出、**I2** 判据不过不提交、**I3** 只有 committed 是断点。
30 条测试，每条不变量都有"必须被拒"的用例。

判据四模板：`write`（文件非空 + 契约覆盖）、`verify`（退出码 + 输出行数 + 耗时）、
`explore`（至少一条证据）、`join`（上游全 committed + 契约全覆盖）。

### kill-and-resume 实验：成立，但收益只说了一半

用真实产物跑：A 提交后"中断"，resume 后 **A 的 mtime 与 sha256 均未变**，`next` 精准指向
B 的 `action: commit`。任务级跳过率 50%。

**墙钟收益没有测**（实验重放的是已完成产物集，没有真实推理，秒数不代表任何事）。
详见 `fd-agent-run/docs/RESUME-EXPERIMENT.md`——那里的局限段比结论段重要。

实验还抓到一个自造缺口：`next` 只认 `pending`，于是停在 `produced` 的任务既不可派发也
不被提示，运行会静默卡住而每个组件都报成功。已修（`action: commit`）。

## 三·十四、第八回合：5 路实验 + fd-fleet 上线（2026-11-09）

### 5 路实验：三条不变量成立，但收益只说对了一半

六任务（5 路由 + 合并点）跑 `fd-fleet`（一条命令看整机）。中断点建在 3 committed / 3 pending，
`resume` 跳过 3 个已提交，最终 6/6 committed。**中断点记录的三份 sources sha256 在全部完成后
逐字节未变** —— 已提交部分确实没被重做。省 3 次派发（50%）。判据求值 6 次（I2 真在跑）。

**诚实边界（写进 `fd-agent-run/docs/RESUME-EXPERIMENT.md`）**：中断是**模拟的**（完成 3 路后
停下再 resume），没有真 SIGKILL 持有租约的进程；9-10 路、多次中断、并发租约冲突都没测；
合并点由主 agent 做。所以"大规模真中断下仍省成本"**没有被证明**。

### 这轮抓到的三个缺陷，全是我自己造的

1. **判据作用域错了**：把 `contract: true`（join 级属性）套给每条路由，于是三条路由产出齐全、
   证据真实却被全部拒绝。**状态机对、判据错**——I2 正确地拒绝了条件未满足的提交。
   顺带确认了件好事：判据记在 `task_state` 里**不能在运行中修改**，改判据必须重开一轮。
2. **`if (effective === null) return;` 提前返回**短路掉了后面所有严重度规则 →
   `ports: 1 unexpected` 被算成 `ok`。
3. **`reduce(fn, null)` 种子为 null**，`RANK[null]` 是 undefined，比较恒假 → 结果永远是 null。

外加一处语义修正：**显式 status 与逐项 severity 取最坏，不是显式优先**——否则自称 `ok`
但某磁盘 `level: degraded` 的报告会被整体判 ok。

### fd-fleet 上线（第 4 个定时任务）

- 目录：目标机 `D:/fd-fleet`（与其它三个工具同级，默认路径是相对同级的 `../fd-*`）
- 任务：`fd-fleet-hourly` -> 跑 `run.cmd` -> 写 `logs/fleet.log`（`--quiet`，只一行 verdict）
- 手工跑：`node src/cli.js`（不带参数即读三处默认日志）

真机一页输出（与 netstat 逐行对过账，68/68 一致）：

```
health: degraded — drives 2, ports 3, images 1
ports: 68 listening, 42 unexpected, 0 image-related, drift +0/-0/~0 over 4 runs
backup: 1 dataset (ok 1), trend aging +0.3d
verdict: degraded — health: degraded; ports: 42 unexpected
```

设计与它服务的四个工具一致：任何组件读不到 → `unavailable: <原因>` 且整体 critical；
rollup 是唯一决定 status 的地方，render 只渲染、CLI 不重算。

## 三·十五、第九回合：让台账不可绕过（2026-11-09）

前面所有工作都有一个共同弱点：**可以不被使用**。台账是可选 CLI，agent 不查它也照样能跑。
这一天里六个"门"的教训就是——**能被绕过的门，最终都会被绕过**。

### 钩子里加了台账守卫

`scripts/dispatch_hook.py` 原本只对账 roster 一致性。现在多一步：

```
无 run                    -> pass，日志写 "ledger ungoverned"（诚实说明没被治理）
有 run 且还有待办          -> pass
有 run 但已全部 committed -> deny（没什么可派了）
FD_AGENT_DISPATCH_UNGOVERNED=1 -> 整段跳过
```

**只认最新的 run**（按 journal mtime）。这条是被 fd-fleet 第一轮撞出来的：那次失败的 run
留下 5 个 `predicate_failed` 任务，被当成"还有活"，于是已完成的项目看起来仍在进行中。
**历史不该治理现在。**

`predicate_failed` 算可派发（失败待重试），`committed`/`abandoned` 不算。

### 钩子绝不写死本机路径（2026-11-14，CI 第一次跑抓到的）

`PKG` 曾经是 `Path("/Users/chengsishi/finddata/fd-agent-graph")`——作者的家目录，也就是
**只有一台机器上成立**。换一台机器，每次检查都抛进"绝不因为检查器坏了而阻塞派发"那个
`except`，于是钩子退出 0、日志一行不写：与"检查过且干净"的输出**逐字节相同**。CI 第一次跑
就有五条用例从 `deny` 变成静默通过。

现在的解析顺序是 `FD_AGENT_GRAPH_PKG` → `Path(__file__).resolve().parents[1]`，并且两种"缺"
被分开报：**包不在** → `skipped`（钩子接在了没有检查器的地方，无事可做）；**包在而 roster
不在** → `ungoverned`（配置过、但有一块丢了）。日志写不进去时也不再静默，改打 stderr——
日志是这条闸唯一的可观察面，写不进去等于"装没装"看不出来。

同一轮还让钩子接受 `FD_AGENT_GRAPH_MCP_CONFIG` / `FD_AGENT_GRAPH_SKILLS_ROOT`（透传给
`bind --mcp-config` / `--skills-root`）。理由不是"方便测试"，而是：roster 声明的是**本工作区**
的服务器与技能，在别的机器上它们本来就不存在，钩子会在与派发毫无关系的原因上拒绝。测试用
`tests/host_fixture.py` 按 roster 自己声明的内容现搭一个宿主（服务器与技能都从 roster 读，
所以往 roster 里加一条绑定不会让 fixture 悄悄不全）。

### 它最可能犯的错，是先写下来再去测

文本匹配是裸子串，实测：

```
"You are the planner."      -> 命中（对）
"role=implementer"          -> 命中（对）
"the merger script broke"   -> 命中（误判：说的是文件）
"这次别 plan 太久"           -> 漏判（plan 不是 planner 的子串）
"explore 目录结构后告诉我"    -> 漏判
```

**没有修**，因为要修就得解析 payload 结构，而那个 schema 未文档化——猜 schema 会在它变的那天
静默失效。所以：把实测结果写进模块 docstring，并加 4 条测试把当前行为钉住（含两条"这就是错的行为"）。
日志同时记录 `not a crew dispatch`，因为**这个设计最可能的失败形态是沉默**。

### 12 条新测试，两条变异验证

- 去掉"只认最新 run" → `test_only_the_newest_run_governs` 必红
- 把 `predicate_failed` 从可派发里去掉 → 对应用例必红

## 四、这次踩过、值得记住的坑（都已在代码或文档里固化）

1. **配置形状**：合法只有 `hooks.events.<Event>`。顶层 `hooks.<Event>` 判 `Unrecognized keys`
   → `Config file failed to load`。本机因此累计 4,968 条失败，其中 **4,890 条早于本次改动**——
   impeccable 的三条钩子从来没工作过。已修，失败归零。
2. **`validate_manifest.py` 会静默退回过时镜像规则**（缺 `tags`）。看到 `using mirrored rules`
   就当没验过；**传绝对路径**，判据是输出里 `validator: real validator`。
3. **首发包不能带 `--pack-id`**（它是「给已存在的包发新版本」，会在最后一步 404）。
4. **谦面包的角色是 persona-only**，装不了 `tools`/`model`——能力绑定发不了包，只能进插件。
5. **`report` / `spec` / `no` 这类通用词会被当成锚点**（曾把 `no` 匹配到 `vendor/echarts.min.js#no`）。
6. **宽度是 roster 的事实，不是模板的**：模板只画一个 `worker`，不按 `parallel` 展开会把 6 路算成 1 路。
7. **「生成」≠「安装」≠「生效」**：三者要分开报，`accept.py` 已分开。
8. **钩子必须自带日志**，否则「没拦」「没装」「配置没加载」长得一模一样。
9. **码表里同一个名字写两遍不会报错**，只会有个语义悄悄失效（`HOST_AGENT_MISSING` 曾 warning 被 error 覆盖）。
10. **验证退出码不要接管道**（`| tail` 会把 rc 换成 tail 的）——本次会话踩了 5 次。
11. **判据本身可能是假的**：钩子日志被单测写入，于是「日志有新增」既可能是真实触发也可能是跑过测试。
    判据要先自证：`FD_AGENT_GRAPH_LOG` 覆盖 + 断言真实日志逐字节不变。
12. **同一份产物在多处存在时，守卫必须覆盖每一处**：技能 4 处、agent 定义 5 处。
    漏掉任何一处，那一处就是「没人 review 却已发布」。
13. **「查不了」与「没问题」必须分开报**：没有 skills root 时的静默跳过，
    会让「本机没配」看起来像「清单全对」。
14. **散文不会被执行，所以它会撒谎**：README 里两轮出现假命令与旧数字，评审都放过。
    `check_docs.py` 让可执行的那一半真的跑，并核对单测数与路径。
15. **门自身也会递归**：`check_docs` 跑全量单测，而单测里有调用它的用例 → 无限递归。
    用 `FD_AGENT_GRAPH_NO_SUITE` 打断；教训是「门里跑被门管的套件」需要显式的环检测。
16. **「合并图」会过期而「各仓图」不会**：`graphify update <repo>` 只刷 `*/graphify-out/`，
    工作区合并图不跟着变；从合并图建索引 → 新文件永远看不见 → `plan` 归属恒为 0。
    解法是 `rebuild --per-repo`（325,099 节点 vs 合并图 286,653）。
17. **被 kill 的测试会留下污染**：`finally` 没跑到，README 里残留探针，导致后续用例误红。
    改文件型测试必须让恢复失败也可见（或先备份到包外再校验）。

---

## 五、已裁决：`fd-code-graph` 移入 attic

2026-11-08 处理完毕。它是**答错题时的产物**（给源码建图，而 graph engineering 指 agent 编排图），
技术上能用但不在主线上：

```
attic/fd-code-graph/            560KB（137MB 生成产物已删，可重建）
attic/fd-code-graph-README.md   为什么在这、怎么恢复、最可能的复用形态
```

移动后复核：**64 单测仍全过**，`ids` 模块往返正常。生成产物用
`cd attic/fd-code-graph && PYTHONPATH=. python3 -m fd_code_graph.cli rebuild` 重建，
但需要一个新鲜的 `graphify-out/graph.json`。

主树里已无 `fd-code-graph/`；除 `attic/` 外无残留引用。两份规格仍在
`openspec/specs/code-graph-service/`、`code-graph-task-anchoring/`（归档规格是历史记录，不改）。
