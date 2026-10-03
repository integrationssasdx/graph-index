# Graph Index

区块链数据索引与 GraphQL 查询服务：实体映射、订阅推送与查询计划。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

## 状态

- `graph-index query-plan`：将 GraphQL 查询编译为实体扫描/连接计划。
- `graph-index subscription-push --schema <schema.graphql> --subscription <subscription.graphql> --variables <variables.json> --events <events.ndjson> [--operation <name>]`：将订阅编译为实体等值过滤与叶字段投影，按序匹配 NDJSON 实体变更事件（INSERT/UPDATE/DELETE），成功时向 stdout 输出 JSONL（字段：`subscription`、`path`、`event`、`entity`、`data`），失败时向 stderr 输出 `{code, message}` 并以退出码 2 结束。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
