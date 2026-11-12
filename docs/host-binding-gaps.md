# 宿主绑定能力 vs 清单表达能力

2026-11-08 实测，**2026-11-09 更正**。结论先说：

清单最早把方向搞反了（建模 skills/MCP/记忆、漏掉 tools/model/thoughtLevel）。
第一版更正又矫枉过正：写成「skills / MCP / 记忆无法按 agent 约束」。

**那句是错的。** 核查 `zcode.cjs` 的 `parseAgentFrontmatter`：它实际读取
`tools`、`disallowedTools`、`skills`、`background`、`injectAgentsMd`、`maxTurns`、
`memory`（并对取值做 `agent_invalid_memory_scope` 校验）、`permissionMode`、`mcpServers`。
也就是说，**这六项都能写进 agent 定义并被宿主执行**，生成器现在也确实在写。

错误的代价很具体：生成器只写 5 个字段，roster 里声明的 skills/MCP/memory 从未生效；
而 `bind --check-host` 只对账三项，看不到这个缺口——两处「看起来对」互相掩护。

## 逐个字段的落地情况

| 维度 | 宿主作用域 | 按 agent 可执行？ | 清单能表达 | 校验能判定 |
|---|---|---|---|---|
| `tools` | agent 定义 frontmatter | **是** | 是 | 只能校验「与宿主定义是否一致」（需读 agent 定义文件） |
| `model` | agent 定义 frontmatter | **是** | 是 | 同上 |
| `thought_level` | agent 定义 frontmatter（`thoughtLevel`） | **是** | 是 | 同上 |
| `skills` | agent 定义 frontmatter（`skills`） | **是**（2026-11-09 更正） | 是 | 能判定与宿主的原名是否一致 |
| `mcp` | agent 定义 frontmatter（`mcpServers`） | **是**（同上；工具级收窄仍走 `tools`） | 是 | 能判定 server 名是否在同一份 frontmatter 里 |
| `memory_scope` | agent 定义 frontmatter（`memory`） | **否 — 取值域不同**（2026-11-12 实测更正） | 是 | 能判定：写进去必被拒（见下条） |
| `max_turns` / `disallowed_tools` | agent 定义 frontmatter | **是** | **否**（roster 尚无字段） | 待补字段后才可校验 |
| `parallel` | 无此概念，是派发方的事实 | — | 是 | 能算宽度与约束承接 |

**2026-11-12 更正 `memory_scope` 一行（原来是"是"）**：宿主的 `memory` 键是**存储位置**枚举
`user|project|local`，而 roster 的 `memory_scope` 是**写面权威** `private|shared`。两者同名不同轴，
把权威值写进位置槽会让整份 agent 定义被宿主以 `agent_invalid_memory_scope` 拒收——**已实测**：四份
roster 的生成物全部带 `memory: private|shared`，全部装不上，而 `bind --check-host` 当时只对账三项，
看不见这个缺口。现在：生成器不再写该键（权威只走正文纪律），`bind` 新增
`binding.host.memory-location-invalid`（error）对账。别再按本表旧结论往 `memory` 里写 authority。


证据：

- agent 定义文件形如 `~/.zcode/cli/plugins/cache/.../agents/visual-judge.md`，frontmatter 有
  `name / description / color / model / thoughtLevel / tools`，正文是 prompt。
- `~/.zcode/cli/agents/` **不是** agent 定义目录，是会话转录（210 个 `sess_*` 子目录）。
- 配置指南的「五类资源」表里没有 Agents 一行；agent 只能由 plugin 贡献，或经
  Settings → Subagents 管理——**没有文档化的 `<repo>/.zcode/agents/` 项目级路径**。

## 因此这份清单现在是什么

**一份描述 + 一致性证明，不是一份约束。**

- 它能拦住的：名字写错（skill/server 不存在）、记忆权威自相矛盾、端口类型不相容、
  合并点丢约束、`parallel` 声明与模板不匹配。
- 它拦不住的：某个 agent 实际用了它没声明的 skill；某个 agent 用了没绑定的 MCP 工具。

第二类要真拦住，唯一的路是**从清单生成 agent 定义文件**（把 `tools` / `model` /
`thoughtLevel` 写进 frontmatter），让宿主的工具白名单生效。skills / MCP / 记忆**也已写进
frontmatter 并被宿主执行**（2026-11-09 更正；早前这段写着"即使生成了也不生效"，是错的）。原文：
也管不住——那是宿主粒度的限制，不是清单的缺陷。

## 一个被这次实测暴露的真缺口

模板只画一个 `worker` 节点，于是：

```
metrics template alone            → width 1   （看着像串行）
metrics template --roster roster  → width 6   （9 个实例的真实形状）
```

**宽度是 roster 的事实，不是模板的事实。** 已修：`--roster` 把节点按
`parallel` 展开后再算。没有这个展开，模板的度量会系统性低报。

## 下一步（按投入产出排序）

1. ~~清单 → agent 定义文件的生成器~~ **已完成**：`export` 子命令。生成 ≠ 安装——宿主
   没有文档化的项目级 agent 目录，安装是 UI 手工步骤。
2. ~~`bind` 读取 agent 定义做双向对账~~ **已完成**（2026-11-08）：

   ```bash
   python3 -m fd_agent_graph.cli bind --manifest roster/finddata-coding.yaml --host-agents <dir>   # 指定目录
   python3 -m fd_agent_graph.cli bind --manifest roster/finddata-coding.yaml --check-host          # 自动发现
   ```

   | 码 | 级 | 含义 |
   |---|---|---|
   | `binding.host.agent-missing` | warning | 声明了 tools/model/思维档，但同名定义没装 → 一项都不生效 |
   | `binding.host.tools-drift` | error | 声明与实际白名单不一致，并列出「声明了但宿主没有」的差集 |
   | `binding.host.model-drift` | error | 模型不一致 |
   | `binding.host.reasoning-drift` | error | 思维档不一致 |

   `agent-missing` 刻意是 warning 而不是 error：**装之前**是正常状态，不该让 CI 永远红；
   而一旦定义存在却对不上，就是 error。
3. ~~装钩子~~ **已完成**（2026-11-08）：`scripts/dispatch_hook.py` + `hooks.events.PreToolUse`
   matcher `^Agent$`，超时 30s。

   | 场景 | 结果 |
   |---|---|
   | 无关 dispatch | 放行（不解析 payload，只在文本里找班底角色名） |
   | 命中班底 + 清单干净 | 放行 |
   | 命中班底 + 清单有 error | **exit 2 + `permissionDecision: deny`**，理由带码与位置 |
   | 清单里有 warning（如未安装） | 放行 |
   | 脚本自己炸了 / 文件不存在 | 放行（`[ ! -f … ] ||` 守卫 + 内部异常兜底） |

   **能力边界**：钩子逐次调用，看不到「一条消息里发了几个 Agent」，所以**拦不住批内并发**
   ——那靠 `may-dispatch`。它拦的是「清单/模板不合格时不该开跑」。

   **配置形状陷阱**：合法形状只有 `hooks.events.<Event>`。最初误以为 `hooks.<Event>` 也可用，
   两处都注册过，实际顶层键会被判 `Unrecognized keys` 并让整份配置加载失败——后来已把顶层键
   全部并入 `hooks.events.*` 并删除，现在 `hooks` 下只有 `enabled` / `maxOutputBytes` / `events`。

   **⚠ 根因已查明（2026-11-08 15:2x）——原配置一直是坏的**

   `~/.zcode/cli/log/zcode-2026-11-08.jsonl` 里有 4,968 条 `config.file.invalid`：

   ```
   message: "Config file failed to load"
   diagnosticMessage: hooks: Unrecognized keys: "PostToolUse", "Stop", "SessionStart"
   ```

   - **4,890 条**从 2026-11-07T16:02Z 起就有（**不是我造成的**）：顶层 `hooks.<Event>` 不是合法形状，
     只有 `hooks.events.<Event>` 是。也就是说 impeccable 那三条钩子**从来没生效过**，配置每次读都在失败。
   - **78 条**从 07:08Z 起多了一个 `"PreToolUse"` —— 那是我加的，我把它弄得更糟。

   已修：把 `SessionStart` / `PostToolUse` / `Stop` 三条顶层条目**合并进 `hooks.events.*`** 并删除顶层键。
   修完 `config.file.invalid` 归零（观测 75 秒无新增），`mcp.servers` 与 `plugins` 完好。

   **✅ 已验证生效（同日 16:41）**

   中间有一段误判：修完配置后派发一次，日志仍无新增，我据此推断「钩子执行器在会话启动时绑定」。
   那次推断是错的——**探针 prompt 里没有班底角色名**，钩子在第一道判断就放行了，本来就该不写行。

   用一条**含 `implementer`** 的派发重测，`build/dispatch-hook.log` 新增：

   ```
   pass	crew dispatch, roster clean
   ```

   所以根因从头到尾只有一个：`hooks: Unrecognized keys` 让整块 hooks 配置加载失败，
   **钩子从来没执行过**——包括 impeccable 那三条（PostToolUse on Edit|Write、Stop、SessionStart）。
   它们现在也合法了。

   判据从此可信：`FD_AGENT_GRAPH_LOG` 覆盖让单测写 `/tmp`，真实日志只可能由真实派发写入。

---

## 六、审计后修掉的沉默缺陷（2026-11-08 四路审计产出）

原则：**「查不了」与「没问题」必须分开报。** 下列每一条原本都是静默通过的。

| 缺陷 | 现在的行为 |
|---|---|
| 端口段不是映射（`inputs: topic`） | `graph.node.malformed-ports`（error），并说明读不出什么 |
| `constraint` 不是布尔（占位符） | 同上，报「constraint is not a boolean」 |
| 边携带了目标没声明的输入 | `graph.edge.value-unconsumed`（warning）——值在消费端消失 |
| `--roster` 展开丢 `unresolved` / `malformed` | 两者都随展开保留，`UNRESOLVED_SUBGRAPH` 不再蒸发 |
| 容量拒绝只写 `reason`、`blocking=()` | 产出 `graph.dispatch.above-width`，CI 可订阅 |
| 坏输入抛 traceback + 退出 1 | 统一退出 2，一句话说明 `malformed input (TypeError): ...` |
| `--concurrency 0/-3` 被判放行 | 退出 2，参数层拒绝 |
| 宿主定义 frontmatter 读不出来 | `binding.host.unreadable`（error）——不再冒充「已装且一致」 |
| 宿主定义不限定工具（`*` 或缺键） | `binding.host.tools-unconstrained`（warning）——声明的白名单没生效 |
| 显式 `--skills-root` 指向不存在目录 | `binding.env.skills-root-missing`（error）；默认根不存在仍只是 warning |
| 子图按 CWD 解析 | `DEFAULT_SUBGRAPH_ROOT = None` → 按定义文件所在目录 |
| 未闭合 flow 集合被当字符串 | `YamlError`，报真实行号 |
| `fg` 命令不存在 | 文档全部改为 `python3 -m fd_agent_graph.cli` |
| 钩子日志被单测污染 | `FD_AGENT_GRAPH_LOG` 覆盖，单测写 `/tmp`，判据可信 |

### 第二批（同日清完）

| 缺陷 | 现在的行为 |
|---|---|
| `bound: -1` 抑制无界环 | 只有 `bound > 0` 才算有界，负值照样报 `unbounded-cycle` |
| `metrics`/`baseline` 静默丢弃 findings | 带 `errors`/`warnings` 计数；有 error 时 stderr 提示「要判词请跑 check」 |
| YAML 重复键 last-wins | `duplicate key 'effects'` + 行号，直接报错 |
| `--json` 错误负载字段不全 | 统一 `{target, exit_code, error, summary, findings}`，CI 不用写兜底 |
| pack skill description 与 SKILL.md frontmatter 不一致 | 已对齐（52→148 字）；`check_copies` 现在也比 frontmatter，漂移必红 |
| `build/agents/` 是无人守卫的第 5 份副本 | 加守卫：必须逐字节等于生成器输出 |
| 两个 roster 的 `template:` 锚定约定不一致 | 统一为「相对清单目录」（`../examples/...`、`../build/...`） |

**仍未修（已知）**：`---` 分隔的多文档仍被静默当作单文档；
`roster` 的 `template:` 是相对路径，**清单被移走就断**（钩子单测的夹具因此要写绝对路径，
已改为自治夹具并在测试里注明）；`--json` 只在 check/bind/export 上被接受，
其余命令本就总输出 JSON，传 `--json` 会因 argparse 退 2。
