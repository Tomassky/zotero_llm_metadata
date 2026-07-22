# zotero_llm_metadata

通过 LLM 自动为 Zotero 中的条目提取元数据、补充摘要、生成标签，并在此基础上构建可查询的知识图谱。项目附带一个 **Claude Code 技能**（`skill/`），可用自然语言只读查询文献库与知识图谱。

![总体架构：批处理增强流水线 + 查询技能](docs/architecture.png)

> 总体架构：左侧为**批处理增强流水线**（`src/zotero_llm_metadata/`），右侧为**查询技能**（`skill/`，Claude Code Skill），两者共享 Zotero 库、本地文件与图谱输出。

## 功能

- 扫描无元数据的独立附件，调用 LLM 生成结构化元数据（标题、作者、摘要、DOI 等），通过 Connector 写入 Zotero 并自动挂载父条目
- 为已有条目补充缺失的 `abstractNote`（支持读取附件全文，或通过 VL 模型识别图片）
- 为无标签条目批量生成标签（读取附件全文，调用 LLM 输出结构化 JSON 标签列表）
- 基于集合与标签关系构建知识图谱，运行 Leiden 社区检测，导出可交互的 HTML 可视化
- 以上元数据/摘要步骤均自动管理 Zotero 进程（写入 SQLite 前关闭，写入完成后重启）
- 提供 **Zotero 技能**（Claude Code Skill）：以自然语言只读查询文献库（实时 API）与知识图谱（离线快照）

**支持格式：**

| 类型 | 格式 |
|------|------|
| 文档 | `.pdf` / `.docx` / `.docm` / `.doc`（需 LibreOffice 或 antiword） |
| 表格 | `.xlsx` / `.xls` / `.csv` |
| 演示 | `.pptx` / `.pptm` / `.ppt` |
| 文本 | `.txt` / `.md` / `.json` / `.rtf` / `.html` / `.htm` |
| 电子书 | `.epub` |
| 文字处理 | `.odt` |
| 图片 | `.png` / `.jpg` / `.jpeg` / `.gif` / `.webp`（需配置 `vl_model`） |

## 安装

项目采用标准 `src/` 包布局，通过 `pip install -e .` 安装为可导入包（提供 `python -m zotero_llm_metadata` 与 `zotero-llm-metadata` 两种入口）：

```bash
# 核心（仅 httpx）
pip install -e .

# 按功能装可选依赖
pip install -e ".[extract]"   # 多格式文本提取 + 图片处理
pip install -e ".[graph]"     # 知识图谱构建与可视化
pip install -e ".[all]"       # 全部功能
```

也可直接按需单独安装底层库：

```bash
pip install httpx pypdf pdfminer.six python-docx openpyxl python-pptx \
            striprtf ebooklib odfpy Pillow networkx pyvis
```

> 除 `httpx` 外均为可选依赖，按需安装：
> - `pypdf` / `pdfminer.six`：PDF 文本提取
> - `python-docx`：Word（`.docx` / `.docm`）文本提取
> - `openpyxl`：Excel 文本提取
> - `python-pptx`：PowerPoint 文本提取
> - `striprtf`：RTF 文本提取
> - `ebooklib`：EPUB 文本提取
> - `odfpy`：ODT 文本提取
> - `Pillow`：图片缩放与编码（图片附件必须）
>
> 老式二进制 `.doc` 需额外安装外部工具之一：
> - **LibreOffice**（跨平台，推荐）：https://www.libreoffice.org
> - **antiword**（macOS / Linux）：`brew install antiword` / `apt install antiword`

## 配置

调用 LLM 需要提供 API Key，**通过环境变量 `DASHSCOPE_API_KEY` 读取**（`src/zotero_llm_metadata/config.py` 中 `api_key=os.getenv("DASHSCOPE_API_KEY", "")`）。请勿将密钥硬编码进源码或提交到版本库。

```bash
# 当前会话临时设置
export DASHSCOPE_API_KEY=sk-xxxx

# 或写入 shell 配置持久化（zsh 示例）
echo 'export DASHSCOPE_API_KEY=sk-xxxx' >> ~/.zshrc && source ~/.zshrc
```

未设置该变量时，凡需要调用 LLM 的模式都会因 Key 为空而失败（`--dry-run` 不受影响）。

其他参数（Zotero API 地址、模型名称、数据库路径、VL 模型等）在 `src/zotero_llm_metadata/config.py` 的 `make_args()` 中直接修改。

## 使用

```bash
# 先 pip install -e . ，然后在仓库根目录执行（产物 graph/ 与 *.jsonl 落在根目录）

# 预览：列出无元数据附件 + 缺摘要条目，不调用 LLM
python -m zotero_llm_metadata --dry-run

# 全流程元数据：提取元数据 → 写入 Zotero → 自动 repair → 重启 Zotero
python -m zotero_llm_metadata --fill-metadata-abstract

# 全流程摘要：生成 abstractNote → 关闭 Zotero → 写入数据库 → 重启 Zotero
python -m zotero_llm_metadata --fill-abstracts

# 批量生成标签：扫描无 tag 条目 → 读取附件全文 → LLM 生成标签
python -m zotero_llm_metadata --fill-tags

# 构建知识图谱：从 SQLite 读取条目 → 构建图 → 社区检测 → 导出 graph/
python -m zotero_llm_metadata --build-graph

# 可视化知识图谱（需先运行 --build-graph）
python -m zotero_llm_metadata.graph.visualize   # 生成 graph/graph_vis.html
```

> 安装后也可用等价的控制台入口 `zotero-llm-metadata --dry-run` 等。不带参数运行显示帮助信息。

## 运行流程

![批处理流水线细化图](docs/zotero-llm-metadata-arch.png)

> 批处理流水线细化：上半部分为 **LLM 增强**（元数据 / 摘要 / 标签），下半部分为**知识图谱构建**；写入 SQLite 前后自动关闭并重启 Zotero。

### `--fill-metadata-abstract`

```
① Zotero 运行中
   扫描无元数据附件 → 提取文本/识别图片 → LLM → Connector 写入
② 自动关闭 Zotero
   从 metadata.jsonl 全量 repair：修复未完成的附件挂载，清理 LLM 标签
③ 自动重启 Zotero
```

### `--fill-abstracts`

```
① Zotero 运行中
   扫描缺摘要条目 → 提取全文/识别图片 → LLM 生成摘要（≥500 字中文）
   结果保存到 fill_abstracts.jsonl
② 自动关闭 Zotero
   将摘要写入 SQLite 数据库
③ 自动重启 Zotero
```

### `--fill-tags`

```
① Zotero 运行中（只读 SQLite，不需要 Zotero 运行也可）
   扫描无 tag 条目 → 读取附件全文（支持全格式）→ LLM 生成标签列表
   结果保存到 fill_tags.jsonl
（需手动将标签写回 Zotero）
```

### `--build-graph`

```
① 不需要 Zotero 运行
   从 SQLite 读取全量条目 → 应用同义词归一化（双语/中文近义词）
   → 按集合（weight=2）和共同标签（weight=1）建立边
   → 过滤高度数节点（max_degree=15）和过于泛化的标签（tag_fraction≤0.20）
   → Leiden 社区检测
② 输出到 graph/ 目录
   graph.json（节点/边/社区数据）、GRAPH_REPORT.md（分析报告）、TAG_FILTER.md
③ 可选：运行 `python -m zotero_llm_metadata.graph.visualize` 生成交互式 HTML（需 pyvis）
```

## Zotero 技能（Claude Code Skill）

`skill/` 目录是一个**自包含的 Claude Code 技能**，让你用自然语言只读查询文献库与知识图谱（例如「搜我 Zotero 里关于红队/LLM 的文献」「哪些论文连接了红队和大语言模型」「显示社区结构」「关于 MCP 最核心的文献是哪篇」）。

所有操作都通过一个包装脚本 `skill/bin/zot <subcommand>`（已安装副本为 `.claude/skills/zotero/bin/zot`），向 stdout 输出 Markdown。技能分两类命令：

**实时命令**（需 Zotero 桌面程序打开，走本地 API `localhost:23119`）：

| 命令 | 作用 |
|------|------|
| `zot search "<query>" [--qmode everything]` | 搜索条目（中文/内容/标签查询请加 `--qmode everything`） |
| `zot tag-search "<tag>" ...` | 按标签检索（取交集，支持 `OR` / `-` 排除） |
| `zot metadata <key>` / `fulltext <key>` | 精确元数据 / 全文 |
| `zot collections` / `collection-items <key>` | 集合层级 / 集合内条目 |
| `zot children <key>` / `annotations` / `notes` | 附件与笔记 / 标注 / 笔记 |
| `zot advanced-search '<conditions-json>'` | 结构化条件检索 |

**图谱命令**（完全离线，读取 `graph/graph.json`，需先 `--build-graph`）：

| 命令 | 作用 |
|------|------|
| `zot graph-search "<query>"` | 关键词检索（含同义词归一）+ 关联社区 |
| `zot graph-community [<label>]` | 社区概览 / 成员 |
| `zot graph-explore <key>` / `graph-neighbors <key>` | 单条目上下文 / 邻居 |
| `zot graph-bridge "<A>" "<B>"` | 连接两个主题的桥接条目 |
| `zot graph-central [--community <label>]` | 最核心/连接最多的枢纽条目 |
| `zot graph-path <keyA> <keyB>` | 两条目间最短路径 |

典型工作流通过 **8 位 item key** 串联：`graph-search` 找到 key → `graph-explore` 看社区/邻居 → `metadata` / `fulltext` 取精确内容。完整命令参考见 [`skill/SKILL.md`](skill/SKILL.md)。

> 技能自带独立虚拟环境（`skill/.venv`，gitignored），唯一外部依赖为 `graph/graph.json`（由 `--build-graph` 生成，可用 `ZOTERO_GRAPH_JSON` 覆盖）；同义词归一已内置于 `skill/scripts/synonyms.py`（`graph/builder.py` 映射表的副本），不再 import 仓库代码。技能同时镜像在 Claude Code 实际加载的 `.claude/skills/zotero/`。

## 输出文件

| 文件 | 内容 |
|------|------|
| `metadata.jsonl` | 每行一条元数据提取结果（含原始响应和写入状态） |
| `fill_abstracts.jsonl` | 每行一条摘要生成结果（含置信度和原始响应） |
| `fill_tags.jsonl` | 每行一条标签生成结果（含 LLM 原始响应） |
| `graph/graph.json` | 知识图谱数据（节点、边、社区） |
| `graph/GRAPH_REPORT.md` | 图谱分析报告（社区概览、关键节点、模块度） |
| `graph/TAG_FILTER.md` | 被过滤掉的高频标签列表 |
| `graph/graph_vis.html` | 交互式可视化（由 `graph/visualize.py` 生成） |
| `graph/lib/` | 可视化 HTML 依赖的 pyvis 本地资源（bindings 等 JS/CSS），与 `graph_vis.html` 同级生成，相对引用自洽 |

## 模块说明

所有流水线代码位于 `src/zotero_llm_metadata/` 包内：

| 文件 | 包含内容 |
|------|---------|
| `__main__.py` | 入口、五种运行模式（dry-run / fill-metadata-abstract / fill-abstracts / fill-tags / build-graph） |
| `config.py` | 参数集中管理（模型、路径、图谱参数等） |
| `runners.py` | 各模式的顶层调度逻辑 |
| `extract.py` | 多格式文本提取（PDF / Word / Excel / PowerPoint / HTML / Markdown / TXT / CSV / JSON / RTF / EPUB / ODT）；图片缩放与 base64 编码 |
| `llm.py` | LLM 调用（含 retry）、JSON 解析（含控制字符修复）、Prompt 构建、VL 图片识别 |
| `tags.py` | 无标签条目扫描、全文提取、LLM 标签生成 |
| `zotero/api.py` | Zotero HTTP API 交互、元数据工具函数 |
| `zotero/db.py` | Zotero SQLite 操作（reparent、apply abstracts、tag cleanup） |
| `zotero/process.py` | Zotero 进程管理（检测、关闭、重启） |
| `graph/builder.py` | 知识图谱构建（读取 SQLite、同义词归一化、建边、度数过滤、Leiden 聚类、JSON/报告导出） |
| `graph/visualize.py` | 从 `graph/graph.json` 生成交互式 HTML（Pyvis，按社区着色） |

## 项目结构

```
zotero_llm_metadata/
├── pyproject.toml          # 包定义（setuptools + src 布局）、依赖、控制台入口
├── requirements.txt        # 依赖清单（等价于 pyproject 的 extras 合集）
├── README.md  LICENSE
│
├── src/zotero_llm_metadata/    # 批处理增强流水线（可导入包）
│   ├── __main__.py         # 入口 + 五种运行模式
│   ├── config.py           # 参数集中管理（模型、路径、图谱参数等）
│   ├── runners.py          # 各模式的顶层调度逻辑
│   ├── extract.py          # 多格式文本提取 + 图片处理
│   ├── llm.py              # LLM / VL 调用、重试、Prompt 构建、JSON 解析
│   ├── tags.py             # 无标签条目扫描 + LLM 标签生成
│   ├── zotero/             # Zotero 集成
│   │   ├── api.py          # HTTP API 交互、元数据工具函数
│   │   ├── db.py           # SQLite 操作（reparent / apply abstracts / tag cleanup）
│   │   └── process.py      # 进程管理（检测 / 关闭 / 重启）
│   └── graph/              # 知识图谱
│       ├── builder.py      # 建图、同义词归一、度数过滤、Leiden 聚类、导出
│       └── visualize.py    # 从 graph.json 生成交互式 HTML（Pyvis）
│
├── skill/                  # Claude Code 技能（自包含，镜像到 .claude/skills/zotero/）
│   ├── SKILL.md            # 技能说明与命令参考
│   ├── bin/zot             # 包装脚本（自动定位 venv，设置 ZOTERO_LOCAL）
│   ├── scripts/            # CLI 代码（cli.py + graph_query / synonyms / local_db 等）
│   ├── requirements.txt
│   └── .venv/              # 技能本地虚拟环境（gitignored）
│
├── docs/                   # 文档资源
│   ├── architecture.png            # 总体架构图（批处理流水线 + 查询技能）
│   └── zotero-llm-metadata-arch.png  # 批处理流水线细化图
│
└── graph/                  # --build-graph 输出目录（gitignored）
    ├── graph.json          # 节点/边/社区数据
    ├── GRAPH_REPORT.md     # 图谱分析报告
    ├── TAG_FILTER.md       # 被过滤的高频标签
    ├── graph_vis.html      # 交互式可视化
    └── lib/                # pyvis 本地资源（bindings 等），与 html 同级
```

> `lib/` 是 pyvis 生成 `graph/graph_vis.html` 时释放的静态资源。`visualize.py` 会从输出目录写出，使其与 html 同级落在 `graph/lib/`，让 html 里的相对引用自洽；整个 `graph/` 已 gitignored。
