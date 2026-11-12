# roster/generated/ —— 每套名册的可安装 agent 定义

这些文件是 `fd-agent-graph export` 从同目录上一级的 `roster/<name>.yaml` **生成**的，
目的是让「名册里声明了某个角色」这件事有一个**能装的东西**对应（否则 `bind --check-host`
会报 `warn binding.agent-missing`：声明了 tools/model/thoughtLevel 却没定义，等于全不生效）。

**它们不是已安装的东西。** 已安装面是 ZCode 插件
`finddata-coding-crew@finddata-official`，那套按 **v2 名册（`finddata-coding`）** 授权，
只额外带 team-v3 的三个独有角色。这里的文件要生效，必须真的装：

- 手工：设置 → Subagents 里逐份导入；或
- 放进一个插件的 `agents/` 目录再 `plugins install`。

## 为什么不是一个目录serve全部

四套名册对**同名角色**的授权不同（例：team-v3 的 merger 只要 `[Read, Write]`、
audit 的 merger 要 `[Bash, Grep, Read]`、v2 的 merger 是 `[Bash, Edit, Grep, Read, Write]`；
team-v3 与 audit 的 planner 都要 `Glob`，v2 的不要）。同一个 agent 文件无法同时满足，
所以按名册各生成一套。

## 重新生成

改了名册就要重新生成，否则这里是旧规格：

```bash
cd fd-agent-graph
for n in audit team-v3 win-health win-health-v2; do
  PYTHONPATH=. python3 -m fd_agent_graph.cli export --manifest "roster/$n.yaml" --out "roster/generated/$n"
done
```

## 判据（发布/安装前必跑）

```bash
PYTHONPATH=. python3 -m fd_agent_graph.cli bind --manifest roster/<name>.yaml \
  [--template examples/<name>.yaml] --host-agents roster/generated/<name>
```

必须 `0 error(s), 0 warning(s)` —— 2026-11-13 实测四套都是 0/0。
`warn binding.agent-missing` 说明名册里有角色没有对应文件；`ERROR binding.host.tools-drift`
的两种方向要分开读：`on the host but not declared` 是越权，`declared but missing on the host`
是欠授权（agent 干不了正文要求的活）。
