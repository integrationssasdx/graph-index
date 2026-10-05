# Graph Index

区块链数据索引与 GraphQL 查询服务：实体映射、订阅推送与查询计划。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

## 状态

- `graph-index query-plan`：将 GraphQL 查询编译为实体扫描/连接计划。
- `graph-index subscription-push --schema <schema.graphql> --subscription <subscription.graphql> --variables <variables.json> --events <events.ndjson> [--operation <name>]`：将订阅编译为实体等值过滤与字段投影（支持叶字段以及经 @link 到达的一层或多层嵌套对象与列表关系字段，含别名与片段展开），按序匹配 NDJSON 实体变更事件（INSERT/UPDATE/DELETE）。处理时先逐行解析校验，再按实体主键维护各实体最新快照（INSERT/UPDATE 保存 after，DELETE 删除 before 对应实体）；嵌套字段按 @link 的 local→target 顺序从截至当前行的最新目标快照构造对象，任一 local 为 null 时关系为 null（非空对象字段则以 EventError 结束）；返回对象列表的 @link 字段按 local 与 target 当前快照值等值匹配（单字段或等长复合字段，target 不必组成目标主键），匹配结果递归投影子选择集并按目标首次 INSERT 或 UPDATE 入库的事件行排序（UPDATE 保留位置，DELETE 后重现排到末尾），无匹配或任一 local 为 null 时输出空数组。成功时向 stdout 输出 JSONL（字段：`subscription`、`path`、`event`、`entity`、`data`，列表字段在 data 中为对象数组），失败时向 stderr 输出 `{code, message}` 并以退出码 2 结束。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
- `@entity` 的 `key` 接收字符串或字符串数组（复合主键，如 `key: ["chain_id", "id"]`）；`@link` 的 `local` 与 `target` 同样各接收字符串或等长字符串数组。复合 key/local/target 均为非空且元素互不相同的字符串数组；空数组、重复元素、缺失字段、非标量字段、混合参数形式或数量不一致均以 `MappingError` 结束。复合连接在计划中以单条 join 输出，`fromField`/`toField` 为按声明顺序排列的字段数组；单字段 `@link` 仍输出字符串字段名。单对象关系（以及 query-plan 中的所有连接）的 `@link` `target` 未按顺序完整指向目标实体主键时以 `InvalidJoin` 结束；subscription-push 的列表关系不要求 target 组成目标主键。
