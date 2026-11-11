# 抽取资料包格式

`workbench.extractor` 把网页中的全部 `application/ld+json` 块抽成确定性的
JSON 资料包（格式标识 `jsonld-subset-extract/1`），JSON-LD 子集边界见
[source-format.md](source-format.md)。同一份页面字节重复运行得到逐字节一致
的输出；同一事实的多份来源以不同溯源的边并存，不去重。

## 顶层结构

| 字段 | 含义 |
| --- | --- |
| `format` | 资料包格式标识 |
| `page_count` / `complete` | 页面数；全部 script 均成功采集才为 `true` |
| `pages` | 每页一份结果（摘要、script 记录、节点、边、列表、null 属性） |
| `nodes` | 跨页按 IRI 汇集后的节点索引 |
| `edges` | 全部带类型的边，按稳定键排序 |
| `lists` | 全部 `@list` 的顺序结构 |
| `null_properties` | 属性位置显式 `null` 的记录（不产生边） |

## 节点身份与空白节点作用域

- 全局 IRI 节点（可解析的绝对/相对 `@id`）跨页汇集 `kinds`，陈述仍各自保留溯源。
- 显式空白标签 `_:name` 的节点 ID 为 `<page_key>#b:<name>`：**同页不同
  script 共享标签作用域，不同页面的 page_key 不同，绝不互通**。
- 没有 `@id` 的匿名节点按页内出现顺序编号 `#b:anon-n`，字段相同也不合并。
- `@list` 单元节点编号 `#b:list-n`，空 `@list` 同样获得列表身份。
- `node.kinds` 取值：`node`、`iri`、`blank`、`labeled`、`anonymous`、
  `list`、`list_cell`。

## 边与值类型

每条边为 `{subject, predicate, object, provenance}`，`object.kind` 区分：

| kind | 含义 | 附带字段 |
| --- | --- | --- |
| `literal` | 普通字面量（含字符串、整数、布尔、浮点） | `value`、`datatype`（XSD IRI）、`language` 恒为 `null` |
| `lang_string` | 带语言标签的字符串 | `value`、`datatype=rdf:langString`、`language` |
| `iri` | IRI 引用值（含 `@type:@id` 术语与 rdf:nil） | `value` 为绝对 IRI |
| `node_ref` | 指向空白/IRI 节点的引用 | `id` 为节点索引中的节点 ID |

普通数组按无序多值处理，重复元素的每个原位置各产生一条边；`@list` 的
顺序与重复由 `lists[].ordered_elements_path` 及 rdf:first/rest 边表达。

## 缺失、空列表、null 与空字符串

四者互不等价：

- **缺失**：JSON 中没有该属性 → 不出现任何记录；
- **显式 null**：`"note": null` → 进入 `null_properties`，不产生边；
- **空字符串**：`{"@value":"","@language":"zh"}` → 一条 `lang_string` 边，
  `value` 为 `""`；
- **空 @list**：`{"@list":[]}` → 一个 `length: 0` 的列表节点与一条指向它的边。

## 溯源与完整性

`provenance` 始终包含 `page`（最终响应 URL）、`crawl_version`（抓取版本）、
`script_index`（页面内 script 序号，从 0 起）、`json_path`（原 JSON 路径，
如 `$.member.name`、`$.@graph[1].steps.@list[0]`）。

`pages[].scripts[]` 中状态为 `isolated` 的块带 `error_code` 与中文原因：

- `invalid_json`：脚本不是合法 JSON；
- `remote_context`：出现远程 `@context`，仅报告未解析、不联网，该块不计入完整采集；
- `unsupported_subset`：上下文循环、未支持关键字、非法值对象等子集外写法。

隔离只影响对应 script，同页其他块照常采集；任一 script 被隔离则页面与资料包
的 `complete` 为 `false`。给 `extract_page` 传 `raw_dir` 后，页面字节与每个
脚本原文经 `workbench.raw_store` 按 SHA-256 摘要留存，摘要写回摘要与
script 记录，可用 `read_raw(raw_dir, key)` 找回并自动核对。
