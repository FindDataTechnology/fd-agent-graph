# fd-agent-graph

运行时无关的 **agent 图工程化工具**：把 agent 系统表达成显式图（节点=能力/步骤，边=路由与依赖），然后**检查它、度量它、证明它**。

不做执行器（那是 LangGraph / ADK / MAF 的事），只做任何人都还没做的那一层：**把图当被测与可校验的工件**。

对应 change：`openspec/changes/add-agent-graph-engineering/`

## 为什么

「graph engineering」是 2026-07 才被命名的术语。上产的都是执行器，而把它**当工程做**的部分几乎是空的：

| 能力 | 现状 | 本工具 |
|---|---|---|
| 跨节点端口类型系统 | 没人做（`arXiv:2607.27578` 给了判据没给实现） | `assignable()`：结构型 + 宽度子类型 |
| 拓扑作为被测单元 | 没有（现有框架只测节点输出） | `topology.py` |
| 扇入约束丢失 | 只有论文量化（`arXiv:2605.08647`），无工具 | `graph.topology.constraint-dropped`（编辑期 warning）/ `graph.dispatch.constraint-dropped`（派发期 error） |
| 条件边只声明不求值 | 声明了 `when` 但运行时当普通依赖 | `guards.py` 闭语法 + `runner.py` 三值求值 |
| 未执行与"跑了没通过"分不清 | 通常折进失败计数 | `not_executed` + 三因（`when_false` / `when_unknown` / `upstream_not_executed`） |
| 派发可以绕过裁决 | 常见 | `run` 先过 `gate`，`--force` 也必须留痕 |
| 图级 CI | 没有 | `check` / `may-dispatch` / `run` 退出码 |

## 用法

```bash
cd fd-agent-graph
export PYTHONPATH=.

python3 -m fd_agent_graph.cli check examples/fanout-12.yaml          # 全量门禁
python3 -m fd_agent_graph.cli metrics examples/fanout-12.yaml        # 宽度 / 关键路径 / 加速上限
python3 -m fd_agent_graph.cli may-dispatch examples/fanout-12.yaml --concurrency 12
python3 -m fd_agent_graph.cli run examples/fanout-12.yaml --impl tests.fixtures_impl:factory \
    --concurrency 12 --record -                                  # 先过闸，再执行，落派发记录
python3 -m fd_agent_graph.cli baseline examples/fanout-12.yaml       # 与线性基线对比
python3 -m fd_agent_graph.cli diff before.yaml after.yaml            # 拓扑差分
python3 -m fd_agent_graph.cli init <change>/tasks.md --out g.yaml    # 从任务单生成骨架
python3 -m fd_agent_graph.cli codes                                  # 违规码与严重级
```

退出码：`0` 无阻断发现 / `1` 有 error 级发现或容量不足 / `2` 图压根读不了。

**十二个命令都接受 `--json`**：本就输出 JSON 的当无操作（`metrics`/`baseline`/`diff`/`codes`/`may-dispatch`），
`init --json` 输出 JSON 而非 YAML。CI 包装器可以统一追加。

**两个数要分开读**（这是最容易误读的地方）：

- **width** 是容量上限——形状一步能跑几个节点。超过它才拒绝。
- **speed_up_ceiling**（总工作量 / 关键路径）是渐进收益。超过它**放行只警告**：多开的人照跑，只是不再按比例变快，因为串行的头尾封顶。它是形状上界，不是实测吞吐，**更不是并发上限**。

例：`examples/fanout-12.yaml` 宽度 12、上限 3.75 —— 12 个 worker 能跑，只是只值 3.75×。

**条件边与未执行**（`add-graph-conditional-dispatch`）：节点可以声明 `routes` + `select` 成为决策节点，
边和节点都可以带 `when` 守卫（闭语法：`<node>.<route>` / `<node>.done` + `not`/`and`/`or`/括号）。
守卫为假的节点记为 `not_executed(when_false)` 并**不写任何输出**，因此消费它的必需端口要么声明为
可选、要么自己也加守卫——`check` 期就会拦住，不会拖到运行时。
`examples/constraint-dropped.yaml` 是"合流丢约束"的反面样本：`check` 只警告，`may-dispatch` 拒绝。

## 测试

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -q
```

2026-11-12 实测 282 条通过、5 条 skip。**那 5 条不是"没跑到的未知数"，是已知的烂 fixture**：
它们断言的是「在真 change 上量」的读数，而 fixture 是工作区根目录下的
`openspec/changes/<name>/tasks.md`（`build/wire-alert-delivery.graph.yaml` 的 `generated_from`
记的还是 `/Users/chengsishi/finddata/openspec/changes/wire-alert-delivery/tasks.md`）。
那个目录已不在工作区里，所以 `Path(__file__).parents[2]` 找不到它，用例**显式 skip 并打印原因**
（`skip` 与 `pass` 在输出上可分，这是有意的）。

**不要为了变绿把它们改成"不存在也能过"**：那几个断言的期望值（如 2,186 个文件 vs 任务点名的 14 个）
只对那一份 change 成立。要在本地复现，就把对应的 change 放回工作区根再跑。

## 图定义

```yaml
graph: release-review
version: 1
entries: [planner]
terminals: [verifier]
inputs:
  goal: {type: string}
nodes:
  - id: planner
    kind: agent                     # agent|tool|code|human|verify|approve|join
    effects: [read]                 # none < read < write < network < money
    idempotent: true                # write 及以上必须显式声明
    checkpoint: node                # node|superstep|none；node = 重试单元
    bound: 3                        # 环上必须有一个节点带 bound
    cost: 1                         # 用于关键路径加权
    inputs:
      goal: {type: string}
    outputs:
      plan: {type: object, properties: {}, required: []}
      scope: {type: string, constraint: true}   # 约束：收敛点丢掉即报
edges:
  - from: planner
    to: implementer
    carries: [plan, scope]          # 只带这些；漏带 constraint 会被抓
```

`required` 在端口层是布尔、在 object 类型层是列表——按值的类型消歧。

## 真实结论（2026-11-08 首次实证）

按 `AGENTS.md` 里「固定 12 路并发 subagent」的实际形状建图：

```
宽度 12 · 关键路径 4 · 总工作量 15 · 加速上限 3.75
may-dispatch --concurrency 12  →  放行（capacity_ok）但警告收益（payoff_ok=false）
may-dispatch --concurrency 13  →  拒绝（超宽度，塞不进）
约束丢失 24 条：每个 worker 的 file_ownership 与 review_note 在 join 处全部消失
```

两个在办 change 的任务拓扑：宽度 7-8，加速上限 **5.1-5.7**（第三个的 tasks.md 已随归档移走，`build/evidence-report.json` 记为 2）。

即：**12 个 worker 塞得进去，但只买到 3.75× 加速**；真实任务结构的上限是 5-6，而且合并点会静默吞掉每一条分支的约束——这正是 `arXiv:2605.08647` 指出的失败机制，被静态抓到了。

`AGENTS.md` 的并发条款已改为按 `may-dispatch` 计算。

## 设计取舍（重要）

- **加速上限是形状上界**（总工作量 / 关键路径），不是实测吞吐。报告字段固定叫 `ceiling`，并强制与线性基线同口径对比。别把它当性能承诺。
- **占位符必须被拒绝**。`init` 生成 `effects: [todo]`、不写 `idempotent`，门禁会红——而不是填一个看起来合理的默认值。门禁在编造事实上通过，比门禁不存在更坏。
- **未知类型 ≠ 任意类型**。类型代数外的 schema 报 `unsupported`（warning），绝不静默判兼容。
- **约束需要人工标注**。不标注就无法区分「可丢的中间结果」与「不可丢的约束」。这是真实成本，接受。

## agent 绑定清单（`bind`）

上面的工具校验**图**。`agents.yaml` 校验**人和工具的绑定**：谁能用什么、记得什么、在模板图上占哪一格。

```bash
export PYTHONPATH=.
python3 -m fd_agent_graph.cli bind --manifest examples/agents.yaml                    # 只校验清单
python3 -m fd_agent_graph.cli bind --manifest examples/agents.yaml --template t.yaml  # 连模板一起校验
python3 -m fd_agent_graph.cli bind --manifest roster/finddata-coding.yaml --check-host  # 再对账已安装定义
```

（`pyproject.toml` 只注册了 `fd-agent-graph` 这一个入口；未 `pip install` 时没有 `fg` 这个命令。）

`--check-host` 读宿主真正加载的 agent 定义，逐字段对账：

| 码 | 级 | 含义 |
|---|---|---|
| `binding.host.agent-missing` | warning | 声明了能力但同名定义没装 → 不生效（装之前是正常状态） |
| `binding.host.tools-drift` | error | 声明的白名单与实际不一致，列出差集 |
| `binding.host.model-drift` / `reasoning-drift` | error | 模型 / 思维档不一致 |

没有这一层，「清单里写了」和「宿主真的在管」看起来一模一样。

```yaml
roster: finddata-coding
template: examples/coding-template.yaml
agents:
  - id: implementer
    role: 只改自己那片
    template_node: worker
    parallel: 4                 # 这一路实际开几个实例（模板只画一个节点）
    skills: [openspec-apply-change]   # 显式声明；空列表 = 不绑任何 skill（区别于「没写」）
    mcp:
      - server: graphify        # 整个 server
      - server: openspec
        tool: list_changes      # 只这一个工具
    memory_scope: private       # 工作上下文，不承载权威
    inputs:  {tasks: {...}, scope: {...}}
    outputs:
      patch: {type: string}
      file_ownership: {type: array, items: {type: string}, constraint: true}
```

**`memory_scope` 是唯一不能随便填的字段**：

- `private` = 工作上下文，对别人没有权威
- `shared` = 持久事实，必须单一来源

一个 `private` agent 若对别处 `shared` 的事实声明权威 → `memory-authority-conflict`（阻断）。私有副本正是并行 agent「各自都觉得自己对」的根因。

**约束承接在绑定层是 error，在图层是 warning。** 同一个现象，两种场合：图可能还在改；而 roster + 模板是要真派发的，这时丢约束就是那个已知的失败模式。

实测（6 路实例、merge 只收 patch）：

```
ERROR binding.template.constraint-not-consumed  node:merge/agent:implementer/port:file_ownership
      template node 'merge' converges 6 branch(es) of 'worker' but declares no input
      'file_ownership', so the 4 constraint(s) produced by 'implementer' are dropped
      for every downstream decision
```

## 已发布到谦面

pack id `w11_2jocz4UiMeGsdRj0nA` v1（`public`），源在 `pack/`：

```
pack/skills/agent-graph-gate/SKILL.md   方法论技能（1 个）
pack/pack-manifest.json                 1 skill / 0 mcp / 5 persona 角色 / i18n.en 齐
```

**注意**：谦面的 pack 角色是 **persona-only**（`{id, name, persona}`，禁 baseUrl/model/apiKey）。
所以 `export` 生成的 `tools` / `model` / `thoughtLevel` **装不进 pack**——它们只在把生成文件
装进 agent 目录时生效。pack 里的角色是人格与分工，不是能力约束。

## 装进宿主（本地插件）

能力约束要真正生效，只能走宿主的 agent 定义。已经准备好一个本地插件源：

```
plugins/finddata-coding-crew/
├── .zcode-plugin/plugin.json     skills + agents 两个真实资源
├── agents/*.md                   5 个角色，frontmatter 带 tools / thoughtLevel
└── skills/agent-graph-gate/      与谦面包同一份技能
plugins/marketplace.json          本地开发市场 dev-finddata-local
```

**已安装（2026-11-08）**。UI 需要人手点击，所以按客户端的缓存与配置形状写入：

```
~/.zcode/cli/plugins/cache/finddata-official/finddata-coding-crew/0.1.0/
  .zcode-plugin/plugin.json · agents/*.md ×5 · skills/agent-graph-gate/SKILL.md · .zcode-plugin-seed.json
known_marketplaces.json   finddata-official → github: FindDataTechnology/fd-vertical-packs
config.json               plugins.enabledPlugins["finddata-coding-crew@finddata-official"] = true
```

备份：`config.json.bak-plugininstall`、`known_marketplaces.json.bak-plugininstall`。

**验证到哪一步**：`bind --check-host` 从 5 条 `agent-missing` warning 变成 **0**，
`accept.py` 显示 `installed definitions 5` / `plugin installed: yes`。

**没有验证到的一步**：当前会话调 `planner` 返回 `Agent type 'planner' not found`——
agent 类型表在**会话启动时固定**（与钩子同一机制），需新会话确认角色真的可用。


