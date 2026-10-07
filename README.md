# Graph Index

区块链数据索引与 GraphQL 查询服务：实体映射、订阅推送与查询计划。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

## 状态

- `graph-index query-plan`：将 GraphQL 查询编译为实体扫描/连接计划。当 schema 的**列表根字段**声明了 `page: Int`、`pageSize: Int`、`orderBy: String`、`sortDirection: String` 四个参数中的任意若干个时，查询可在该字段上提供对应参数；字段未声明的参数名仍按既有未知参数规则结束。每个根条目额外输出 `page`、`pageSize`、`orderBy`、`sortDirection`：`page` 缺省为 `0`，`sortDirection` 缺省为 `ASC`，`orderBy` 缺省（或缺省值）为 `null`（表示保持入库顺序），`pageSize` 缺省为 `null`。
- `graph-index subscription-push --schema <schema.graphql> --subscription <subscription.graphql> --variables <variables.json> --events <events.ndjson> [--operation <name>]`：将订阅编译为实体等值过滤与字段投影（支持叶字段以及经 @link 到达的一层或多层嵌套对象字段与嵌套列表字段，含别名与片段展开），按序匹配 NDJSON 实体变更事件（INSERT/UPDATE/DELETE）。处理时先逐行解析校验，再按实体主键维护各实体最新快照（INSERT/UPDATE 保存 after，DELETE 删除 before 对应实体）；对象字段按 @link 的 local→target 顺序从截至当前行的最新目标快照取单个对象，任一 local 为 null 时关系为 null（非空对象字段则以 EventError 结束）；列表字段将目标表当前快照中 target 值与源 local 值等值匹配的实体全部收集，按目标首次 INSERT/UPDATE 入库的顺序排列（UPDATE 原位替换，DELETE 后重新 INSERT 排到末尾），任一 local 为 null 或无匹配时输出空数组，列表 @link 的 target 不必是目标实体主键（但 local/target 字段均须为标量）。除根实体自身的 INSERT/UPDATE/DELETE 外，订阅选择树经单对象或列表 @link 可达的任一实体发生事件时，还会在该事件之后为应用事件后仍匹配过滤条件、且所选投影实际发生变化的根实体快照追加一条记录：`entity` 固定为订阅根实体表名、`event` 固定为 `UPDATE`，`subscription` 与 `path` 沿用原订阅与响应路径，`data` 为变化后的完整投影；同一事件影响多个根实体时按这些根实体当前快照的稳定入库顺序输出，投影没有实际变化（如仅改动未投影字段）时不追加。根实体自身事件的行为不变（包括根实体表也经自引用 @link 可达时）；过滤器不匹配的根快照既不投影也不产生追加记录。成功时向 stdout 输出 JSONL（字段：`subscription`、`path`、`event`、`entity`、`data`），失败时向 stderr 输出 `{code, message}` 并以退出码 2 结束（stdout 不输出部分结果）。
- `graph-index query-exec --schema <schema.graphql> --query <query.graphql> --variables <variables.json> --events <events.ndjson> [--operation <name>]`：对实体变更快照直接执行一次 GraphQL 查询。事件先逐行解析校验，再按 INSERT、UPDATE、DELETE 顺序维护各查询可达实体的主键当前快照（INSERT/UPDATE 保存 after，DELETE 按 before 移除）；只执行 query，mutation/subscription 以 `UnsupportedOperation` 结束。根参数按映射实体标量字段等值过滤：列表根返回全部匹配行（快照首次入库顺序，UPDATE 原位替换，DELETE 后重插排末尾；声明并提供排序/分页参数时见下节「列表根排序与分页」），单实体根零匹配返回 `null`、多匹配以 `QueryError` 结束。单对象 @link 的 local 按声明顺序指向目标实体主键：任一 local 为 null 时关系为 null（字段非空则 `EventError`），local 非 null 而目标不存在亦为 `EventError`；列表 @link 以 local 与 target 标量值等值匹配（target 无须是主键），无匹配或 local 含 null 返回空数组，并按目标首次入库顺序排列。别名、变量默认值、片段展开与复合主键沿用 query-plan 语义。除实体根字段外，query 文档还支持 GraphQL 内省：根级 `__schema`、`__type(name: String!)`，以及任意对象选择集中的 `__typename`（详见下节「GraphQL 内省」）。事件结构、op、before/after、实体主键或投影快照（含字段值与 schema 声明形状不兼容）不合约定以 `EventError` 结束；GraphQL、变量、映射与连接声明的非法输入沿用既有错误码。成功时向 stdout 输出一次 GraphQL 响应（顶层 `data`），失败时向 stderr 输出单个 `{code, message}` 并以退出码 2 结束（stdout 无部分结果）。

## GraphQL 内省

`query-exec` 在不改动实体查询入口与错误码的前提下实现标准 GraphQL introspection（`query-plan` 与 `subscription-push` 的既有行为不变，不识别这些元字段）：

- `__schema` 返回 `__Schema`：`queryType`/`mutationType`/`subscriptionType`（未声明的根类型返回 `null`）、`types`（全部内置标量、自定义标量、object、interface、union、enum、input object 及内省元类型，顺序确定、不重复）与 `directives`（内置 `skip`/`include`/`deprecated`/`specifiedBy` 加 SDL 中声明的 directive，含 `args`、`locations`、`isRepeatable`）。
- `__type(name: String!)` 按名称返回对应 `__Type`，未知名称返回 `null`。各类型 `kind`（SCALAR/OBJECT/INTERFACE/UNION/ENUM/INPUT_OBJECT/LIST/NON_NULL）、`name`、LIST/NON_NULL 的 `ofType` 包装结构，以及可展开关系均为确定结果：object/interface 的 `fields`（含参数 `args`、参数 `defaultValue` 字符串、字段 `type` 包装）与 `interfaces`；interface/union 的 `possibleTypes`；enum 的 `enumValues`；input object 的 `inputFields`。与当前类型 kind 不适用的关系字段返回 `null`；未请求的字段一律不出现在结果中。`defaultValue` 以 GraphQL 常量值语法的字符串形式给出。描述与弃用未在 SDL 中建模，故 `description`/`deprecationReason`/`specifiedByURL` 为 `null`，`isDeprecated` 恒为 `false`，`includeDeprecated` 参数被接受但不影响结果。
- `__typename` 可出现在任意对象（含 query 根、内省元类型对象）选择集中，返回该对象类型的名称；别名、片段展开沿用实体选择的既有合并语义。
- `__schema` 不接受实参且必须带 selection set；`__type` 必须提供 `name` 且不接受其他实参，`name` 字面量必须是 String（枚举名/数字/布尔等非 String 字面量）或类型为 String 的变量；内省字段上出现未声明实参、缺少 selection set、在非根对象上选择 `__schema`/`__type`、给 `__typename` 传参数或附加 selection set，均以 `InvalidQuery` 结束（元类型上的未知字段仍为 `UnknownField`）。`name` 由变量提供但变量缺失、未声明或类型不符时以 `VariablesError` 结束。
- 内省与 `__typename` 在复杂度控制关闭时同样可用；控制开启时复杂度均为 0，已有 `QUERY_COMPLEXITY_EXCEEDED` 判定不变。只含根级内省/`__typename` 的查询仍要求 `--events` 文件可读，但不解析也不折叠其中事件；与实体根字段混合时，实体部分继续按当前快照、连接与顺序执行，事件仍逐行解析校验。

## 列表根排序与分页

当 schema 的列表根字段声明了 `page: Int`、`pageSize: Int`、`orderBy: String`、`sortDirection: String`（通常为可空）时，`query-plan` 与 `query-exec` 在该字段上接受这四个参数；未声明的参数名仍走既有未知参数规则。`subscription-push` 与单实体根、嵌套列表 @link 字段不识别这些参数，行为不变。

- `query-plan` 在每个根条目输出 `page`、`pageSize`、`orderBy`、`sortDirection` 四个键：`page` 默认 `0`，`sortDirection` 默认 `ASC`；未提供 `orderBy` 时为 `null`（保持入库顺序），未提供 `pageSize` 时为 `null`（不截断）。
- `query-exec` 先按既有规则解析文档与变量、折叠事件快照并完成等值过滤，**仅**对每个列表根字段依次执行排序与分页；多个根字段分别排序分页，互不影响；嵌套列表 @link 的连接顺序保持不变。
- `orderBy` 只接受根实体上的**非列表、非空** `Int`、`Float`、`String`、`ID`、`Boolean` 或 enum 字段：`Int`/`Float` 按数值比较，`String`/`ID` 按 Unicode 码点比较，`Boolean` 为 `false` 先于 `true`，enum 按 SDL 声明顺序比较。排序值为 `null` 时无论 `ASC`/`DESC` 一律排最后；`ASC` 由小到大，`DESC` 由大到小。排序值相同的行先按实体主键声明顺序比较，仍相同时保留入库顺序（UPDATE 原位替换，DELETE 后重新 INSERT 的行落到入库顺序末尾）。
- `page` 为零基页码，`pageSize` 必须为正整数；只给 `page` 不给 `pageSize` 以 `InvalidQuery` 结束；`pageSize` 缺省时不截断（可单独使用 `orderBy`/`sortDirection`，或只用 `pageSize` 取首页切片）。页码超出范围时返回空数组或尾部不足一页的部分。
- 非法输入：`page < 0`、`pageSize < 1`、`orderBy` 指向未知/可空/列表/复合（对象、接口、联合、非标量）字段、`sortDirection` 不是 `ASC` 或 `DESC`、以及无 `orderBy` 却提供 `sortDirection`——静态字面量以 `InvalidQuery` 结束；由变量提供时取值或类型不合法以 `VariablesError` 结束。失败时向 stderr 输出单个 `{code, message}`、退出码 2，stdout 无结果。

## 查询复杂度控制

通过环境变量 `GRAPHQL_QUERY_COMPLEXITY_LIMIT` 在服务启动时开启：变量缺省或值为 `0` 时功能关闭；值为正整数时为单次 operation 的最大复杂度；负数、非整数或无法解析的值使启动失败（不执行任何命令），向 stderr 输出 `{code: "INVALID_QUERY_COMPLEXITY_LIMIT", message}` 并以退出码 2 结束。

复杂度在真正执行解析器、读取索引实体或建立订阅之前，按同一份 GraphQL 文档与变量、对请求选定的单个 operation 计算（未选中的 operation 不计入）：

- 从 operation 根 selection set 开始；每个叶子字段计 1，组合字段只累计其子 selection（对象容器自身计 0）；
- 重复字段与不同 alias 分别计分（即便执行阶段按 response key 合并，复杂度仍按原始选择逐个计）；
- fragment 在展开位置计入一次（同一 fragment 展开两处即计两处），inline fragment 直接计入其选择；类型条件不匹配当前类型的片段计 0；
- 内省字段 `__schema`、`__type` 以及隐式元字段 `__typename` 计 0；
- 列表字段按有效条目上界放大其子 selection：优先读取字段参数 `first`，其次 `limit`，再次（列表根字段声明并提供的）`pageSize` 的正整数面值，也接受对应变量（含变量默认值）的正整数值；参数缺失、值非正或非整数时按 `1000` 计。变量本身的 GraphQL 类型校验仍由既有校验阶段负责（类型不符仍为 `VariablesError`）。嵌套列表的上界逐层相乘。

判定确定：复杂度等于上限时允许执行并沿用既有查询路径、数据结构与顺序；严格大于上限时不调用解析器、不读取索引实体（query-exec 不再折叠事件）、不建立订阅（subscription-push 不产生任何推送），也没有持久化副作用。此时 query-exec 在 stdout 输出一次 GraphQL 响应、退出码 0：`data` 为 `null`，`errors` 恰有一个，其 `extensions.code` 为 `QUERY_COMPLEXITY_EXCEEDED`；subscription-push 以同一 GraphQL 响应代替 JSONL 输出。查询计划产生的分批取数不重复计费，订阅建立后推送的数据也不再次计费。

畸形文档、变量类型错误、未知字段或 fragment 循环继续返回既有 GraphQL 校验错误（如 `ParseError`/`VariablesError`/`UnknownField`/`InvalidQuery`），不会被改写为复杂度错误；这些校验先于复杂度判定。`first`/`limit` 参数仅在复杂度控制开启、且字段为列表类型时被接受（不计入实体过滤）；功能关闭时仍按原规则报未知/不支持参数。`query-plan` 子命令不实施复杂度控制。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
- `@entity` 的 `key` 接收字符串或字符串数组（复合主键，如 `key: ["chain_id", "id"]`）；`@link` 的 `local` 与 `target` 同样各接收字符串或等长字符串数组。复合 key/local/target 均为非空且元素互不相同的字符串数组；空数组、重复元素、缺失字段、非标量字段、混合参数形式或数量不一致均以 `MappingError` 结束。复合连接在计划中以单条 join 输出，`fromField`/`toField` 为按声明顺序排列的字段数组；单字段 `@link` 仍输出字符串字段名。`@link` 的 `target` 未按顺序完整指向目标实体主键时以 `InvalidJoin` 结束（query-plan 与 subscription-push 的单对象关系均如此）；subscription-push 的列表关系改为等值匹配，target 可以不是主键，只要求对应字段存在且为标量。
