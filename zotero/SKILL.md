---
name: zotero
description: >-
  Query the user's Zotero research library and its knowledge graph / 查询用户的
  Zotero 文献库及其知识图谱. Use for any request about their saved papers, articles,
  PDFs, notes, annotations, tags, collections, or the relationships between them —
  e.g. "search my Zotero for 红队/LLM", "what's in my library about 渗透测试",
  "which papers bridge red-team and 大语言模型", "show the community structure",
  "find the most central paper on MCP", "get metadata/full text/annotations for
  item X", "我的文献里关于…". Covers both live Zotero lookups (titles, metadata,
  full text, annotations, notes, collections, tags) and offline knowledge-graph
  queries (search, communities, bridges, centrality, neighbors, shortest path).
---

# Zotero library & knowledge-graph skill / Zotero 文献库与知识图谱技能

Read-only access to the user's Zotero library plus a pre-built knowledge graph.
本技能只读访问用户的 Zotero 文献库，并查询一个预先构建的知识图谱。

Everything runs through one wrapper / 所有操作都通过一个包装脚本：

```
.claude/skills/zotero/bin/zot <subcommand> [args]
```

`zot` prints Markdown to stdout. Always call it via Bash with the path above.
`zot` 向 stdout 输出 Markdown；请始终用上面的路径通过 Bash 调用（它会自动定位
虚拟环境并设置 `ZOTERO_LOCAL=true`）。

---

## Prerequisites / 前置条件

Check these when a command returns empty or errors. 命令返回空或报错时先查这几项。

- **Live Zotero commands** (search, metadata, fulltext, collections, tags,
  annotations, notes, libraries, advanced-search) need the **Zotero desktop app
  running** — they talk to its local API at `localhost:23119`.
  **实时命令**需要 **Zotero 桌面程序处于打开状态**（走本地 API `localhost:23119`）。
  连接失败通常就是程序没开，请让用户打开 Zotero。
- **Graph commands** (`graph-*`) are **offline** — they read `graph/graph.json`.
  **图谱命令**完全离线，读取 `graph/graph.json`。若提示 "No graph found"，先构建：
  ```
  python __main__.py --build-graph      # run from the repo root / 在仓库根目录运行
  ```
- All access is **read-only** — nothing here modifies the library.
  所有操作均为**只读**，不会修改文献库。

---

## Live vs graph — which to use / 实时查询 vs 图谱查询

| Question / 问题 | Use / 用 |
|---|---|
| Exact metadata / full text / annotations of a paper　精确元数据/全文/标注 | **live** `metadata` `fulltext` `annotations` `notes` |
| "Do I have anything on X?" / keyword　是否有关于 X 的条目/关键词 | **live** `search` (fresh) 或 **graph** `graph-search`（带关联） |
| Topic clusters / themes　主题聚类/有哪些主题 | **graph** `graph-community` |
| "What connects A and B?"　A 和 B 之间有什么联系 | **graph** `graph-bridge` |
| Most important / central paper　最核心/最重要的文献 | **graph** `graph-central` |
| "What's related to this paper?"　与某文献相关的 | **graph** `graph-neighbors` / `graph-explore` |
| "How are these two connected?"　这两篇如何相连 | **graph** `graph-path` |

The **graph** is best for *relationships, structure, discovery*; **live** is best
for *precise, current content*. 图谱擅长**关联、结构、发现**；实时擅长**精确、最新
的内容**。The graph is a snapshot — if the user just added items, prefer live
search or rebuild. 图谱是快照，若用户刚新增条目，优先实时搜索或重建图谱。

---

## Typical workflow / 典型工作流：topic → items → detail

The graph and live tools chain through the **8-char item key** (shown in `[...]`).
图谱与实时工具通过 **8 位 item key**（显示在 `[...]` 中）串联：

1. `zot graph-search "大语言模型"` → find items + communities, note a key like `[3ZE5ARSS]`
   找到条目与社区，记下某个 key
2. `zot graph-explore 3ZE5ARSS` → community, tags, neighbors / 社区、标签、邻居
3. `zot metadata 3ZE5ARSS` → full title/authors/abstract from the live library / 实时元数据
4. `zot fulltext 3ZE5ARSS` → extracted full text if needed / 需要时取全文

---

## Command reference / 命令参考

### Knowledge graph (offline) / 知识图谱（离线）

```
zot graph-search "<query>" [--top-n 10]
```
Keyword search over titles, tags, abstracts and community labels. The query is
**synonym-normalized** (`LLM`→`大语言模型`, `RAG`→`检索增强生成`, `red team`→`红队技术`)
and matched against **every** relevant community; each row includes the item key.
搜索标题/标签/摘要/社区标签；query 会**同义词归一**，并返回**所有**匹配社区，每行带 key。

```
zot graph-community [<label>] [--top-n 15]
```
No label → overview of all communities. With a label → members (with keys) of
every matching community. 无参数→所有社区概览；带标签→每个匹配社区的成员（含 key）。

```
zot graph-explore <item_key> [--neighbor-limit 10]
zot graph-neighbors <item_key> [--by-community] [--limit 20]
```
Full context / neighbor list for one item; `--by-community` groups neighbors.
单个条目的完整上下文/邻居列表；`--by-community` 按社区分组。

```
zot graph-bridge "<topic_a>" "<topic_b>" [--top-n 10]
```
Items linking two topics: direct cross-topic edges **and** connector items that
neighbor both. 连接两个主题的条目：直接跨主题边 **+** 同时邻接两侧的桥接条目。

```
zot graph-central [--community "<label>"] [--top-n 10]
```
Most-connected items (hubs), whole-graph or within a community.
连接最多的条目（枢纽），全图或指定社区内。

```
zot graph-path <key_a> <key_b>
```
Shortest path between two items. 两个条目之间的最短路径。

### Live Zotero (needs the app running) / 实时 Zotero（需程序打开）

```
zot search "<query>" [--qmode titleCreatorYear|everything] [--item-type -attachment] [--limit 10] [--tag T]
```
> **Chinese / content / tag queries: add `--qmode everything`.**
> **中文/内容/标签查询请加 `--qmode everything`。** 默认 `titleCreatorYear` 只匹配
> 标题、作者、年份，常漏中文短语或内容词；`everything` 会搜全文和标签。

```
zot tag-search "<tag>" ["<tag2>" ...] [--limit 10]     # tags ANDed; "a OR b" and "-c" supported / 标签取交集，支持 OR 和 - 排除
zot metadata <item_key> [--format markdown|bibtex] [--no-abstract]
zot fulltext <item_key>                                # indexed full text, else download+convert / 索引全文，否则下载转换
zot collections [--limit N]                            # hierarchical list with keys / 层级列表（含 key）
zot collection-items <collection_key> [--limit 50]
zot children <item_key>                                # attachments + notes / 附件与笔记
zot tags [--limit N]                                   # all tags, grouped A–Z / 全部标签，按字母分组
zot annotations [--item-key K] [--pdf] [--limit N]     # --pdf enables direct PDF extraction / 启用 PDF 直接提取兜底
zot notes [--item-key K] [--limit 20] [--no-truncate]
zot search-notes "<query>" [--limit 20]                # searches notes + annotations / 搜索笔记与标注
zot libraries                                          # user/group/feed libraries / 用户/群组/订阅库
zot advanced-search '<conditions-json>' [--join all|any] [--sort-by F] [--sort-direction asc|desc] [--limit 50]
```

`advanced-search` conditions are a JSON list; each is
`{"field":..., "operation":..., "value":...}`.
`advanced-search` 的条件是 JSON 列表，每项为 `{"field", "operation", "value"}`。
Fields / 字段: `title`, `creator`, `tag`, `year`, `itemType`, `date`, `DOI`,
`abstractNote`. Operations / 操作: `is`, `isNot`, `contains`, `doesNotContain`,
`beginsWith`, `endsWith`, `isGreaterThan`, `isLessThan`, `isBefore`, `isAfter`.
Example / 示例:
```
zot advanced-search '[{"field":"tag","operation":"contains","value":"agent"},{"field":"year","operation":"isGreaterThan","value":"2023"}]' --join all
```

---

## Presenting results / 结果呈现

- Item keys are for chaining tool calls — when answering the user, lead with
  titles/authors and surface keys only if they'll act on them.
  item key 用于串联调用；回答用户时以标题/作者为主，仅在用户需要操作时给出 key。
- Community labels are two distinctive tags joined by ` / ` (e.g. `红队技术 / 进程注入`)
  and are de-duplicated; disambiguate by the `#id` when needed.
  社区标签是两个区分度高的标签以 ` / ` 连接（已去重），必要时用 `#id` 区分。
- If graph results look stale vs what the user describes, offer to rebuild
  (`python __main__.py --build-graph`) or fall back to live `search`.
  若图谱结果与用户描述不符，建议重建图谱或回退到实时 `search`。

---

## Troubleshooting / 故障排查

- **Live command fails / empty but you expect hits** → Zotero app likely closed
  (start it), or the query was too narrow: retry `search` with `--qmode everything`.
  **实时命令失败/为空但预期有结果** → 多半 Zotero 没开，或 query 太窄，改用 `--qmode everything`。
- **`graph-*` says "No graph found"** → run `python __main__.py --build-graph`.
  **图谱命令提示未找到图谱** → 运行 `python __main__.py --build-graph`。
- **Wrong library / 0 items in `libraries`** → the CLI reads
  `~/Nextcloud/Zotero/zotero.sqlite` by default; override with `ZOTERO_DB_PATH`.
  **库不对/`libraries` 为 0** → 默认读 `~/Nextcloud/Zotero/zotero.sqlite`，可用 `ZOTERO_DB_PATH` 覆盖。
- **Need to see errors** → the wrapper sends stderr to `.zot-cli.log` in this skill
  dir; for live debugging run `.venv/bin/python scripts/cli.py <args>`.
  **需要看错误** → wrapper 把 stderr 写到本目录 `.zot-cli.log`；调试可直接跑
  `.venv/bin/python scripts/cli.py <args>`。

---

## Layout / 目录结构 (self-contained / 自包含)

```
.claude/skills/zotero/
  SKILL.md            # this file / 本文件
  bin/zot             # wrapper / 包装脚本
  requirements.txt    # runtime deps / 运行期依赖
  scripts/            # the CLI code / CLI 代码 (cli.py + modules)
  .venv/              # skill-local venv / 技能本地虚拟环境 (gitignored)
```

External deps: only the repo's `graph/graph.json` (built by
`python __main__.py --build-graph`) and `graph_builder.py` (reused for synonym
normalization). 外部依赖仅：仓库的 `graph/graph.json` 与 `graph_builder.py`（复用其
同义词归一）。

Recreate the venv / 重建虚拟环境:
```
cd .claude/skills/zotero
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```
