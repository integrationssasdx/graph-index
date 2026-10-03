# Graph Index

区块链数据索引与 GraphQL 查询服务：实体映射、订阅推送与查询计划。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

## 状态

- `graph-index query-plan`：把 GraphQL 查询编译为实体扫描/连接计划。
- `graph-index subscription-push`：按订阅过滤 events.ndjson 实体变更，stdout 输出 JSONL 通知。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
