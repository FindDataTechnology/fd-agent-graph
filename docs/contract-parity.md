# 外部节点契约 ↔ 本包落点

这张表回答一个问题：**外部那份 DAG 节点契约的每个字段，在这里是实现了、新加了、还是明确不做？**
没有第四种状态。它来自 `add-graph-conditional-dispatch` 的 design（已归档：
`openspec/changes/archive/2026-11-14-add-graph-conditional-dispatch/design.md`），
归档后搬到这里——归档目录带日期，测试不能拿它当稳定输入。

`tests/test_contract_parity.py` 读这张表：**改表就要改代码，改代码就要改表**，两边对不上测试就红。

| 外部契约字段 | 我们的落点 | 状态 |
|---|---|---|
| `id` | `Node.id` | 已有 |
| `subagent` | `binding` 的 roster 行 | 已有 |
| `depends_on` | `Edge.source/target` | 已有 |
| `inputs`（literal / `{node}` / `{file}`） | 类型化 `Port` + `Edge.carries` | 已有（更强，带类型） |
| `prompt_template` | 不在 IR 内——模板归 binding 的 template | 显式拒绝（职责不同） |
| `skills`（省略/`[]`/列表） | `binding` 的技能面 | 已有 |
| `mcps` | `binding` 的 MCP 面 + 工具白名单 | 已有 |
| `instance`（复用子代理会话） | 无 | **显式拒绝**：会话复用属 runner 外部状态，本轮不做 |
| `routes` + `select` | `Node.routes` + `Node.select` | **新增** |
| `when`（守卫） | `Edge.when` / `Node.when` → 闭语法 `Guard` | **新增** |
| 未执行三因 | `not_executed(reason)` | **新增** |
| charter（只收窄） | `effective_face()` | **新增** |
| `background` | 不在 IR 内——派发参数归 CLI | 显式拒绝（不是图语义） |
| 产物寻址 `@nodes/`、`{{ x.output_path }}` | 无 | **显式拒绝**：我们走类型化端口，产物地址是模板层的事 |

## 四条"显式拒绝"的理由，写清楚免得下个人以为是漏了

- **`prompt_template`**：模板是绑定层的事（`binding` 的 template 字段），进了 IR 就会让"图"同时
  承担"这段话说给哪个 agent"和"节点之间怎么连"两件事，而后者才是这张图能被静态判定的原因。
- **`instance`**：复用子代理会话意味着节点带外部可变状态，`not_executed` 与重试语义都会跟着变。
  本轮不做，且**不做是有意的**：先让无状态版本可信，再谈复用。
- **`background`**：它是调用方式（`run --background`），不是图的性质。同一个图前台后台跑，
  形状与约束都一样；把它写进 IR 会让"这张图对不对"多出一个与图无关的维度。
- **产物寻址**：我们传的是**类型化端口**，下游拿到的不是"某个目录里的文件名"而是有类型的值。
  要传路径就声明一个 `string` 端口放路径——这是显式的，不需要另一套地址语法。

## 为什么这张表要活着，而不是一次性文档

字段对照只会漂移，不会消失：外部契约加一个字段，我们这边要么实现要么拒绝，**沉默是第三种答案，
而沉默正是这个包要消灭的东西**。表在，漂移就是红灯；表不在，漂移就是"看起来没变化"。
