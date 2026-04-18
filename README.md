# zotero_llm_metadata

通过 LLM 自动为 Zotero 中的条目提取元数据、补充摘要。

## 功能

- 扫描无元数据的独立附件，调用 LLM 生成结构化元数据（标题、作者、摘要、DOI 等），通过 Connector 写入 Zotero 并自动挂载父条目
- 为已有条目补充缺失的 `abstractNote`（支持读取附件全文，或通过 VL 模型识别图片）
- 为无标签条目批量生成标签（读取附件全文，调用 LLM 输出结构化 JSON 标签列表）
- 基于集合与标签关系构建知识图谱，运行 Leiden 社区检测，导出可交互的 HTML 可视化
- 以上元数据/摘要步骤均自动管理 Zotero 进程（写入 SQLite 前关闭，写入完成后重启）

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

## 依赖

```bash
pip install httpx pypdf pdfminer.six python-docx openpyxl python-pptx \
            striprtf ebooklib odfpy Pillow
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

```bash
export DASHSCOPE_API_KEY=sk-xxxx
```

其他参数（Zotero API 地址、模型名称、数据库路径、VL 模型等）在 `__main__.py` 的 `_make_args()` 中直接修改。

## 使用

```bash
# 在 zotero_llm_metadata/ 同级目录执行

# 预览：列出无元数据附件 + 缺摘要条目，不调用 LLM
python __main__.py --dry-run

# 全流程元数据：提取元数据 → 写入 Zotero → 自动 repair → 重启 Zotero
python __main__.py --fill-metadata-abstract

# 全流程摘要：生成 abstractNote → 关闭 Zotero → 写入数据库 → 重启 Zotero
python __main__.py --fill-abstracts

# 批量生成标签：扫描无 tag 条目 → 读取附件全文 → LLM 生成标签
python __main__.py --fill-tags

# 构建知识图谱：从 SQLite 读取条目 → 构建图 → 社区检测 → 导出 graph/
python __main__.py --build-graph

# 可视化知识图谱（需先运行 --build-graph）
python graph_visualize.py          # 生成 graph/graph_vis.html
```

不带参数运行显示帮助信息。

## 运行流程

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
③ 可选：运行 graph_visualize.py 生成交互式 HTML（需 pyvis）
```

## 输出文件

| 文件 | 内容 |
|------|------|
| `metadata.jsonl` | 每行一条元数据提取结果（含原始响应和写入状态） |
| `fill_abstracts.jsonl` | 每行一条摘要生成结果（含置信度和原始响应） |
| `fill_tags.jsonl` | 每行一条标签生成结果（含 LLM 原始响应） |
| `graph/graph.json` | 知识图谱数据（节点、边、社区） |
| `graph/GRAPH_REPORT.md` | 图谱分析报告（社区概览、关键节点、模块度） |
| `graph/TAG_FILTER.md` | 被过滤掉的高频标签列表 |
| `graph/graph_vis.html` | 交互式可视化（由 `graph_visualize.py` 生成） |

## 模块说明

| 文件 | 包含内容 |
|------|---------|
| `__main__.py` | 入口、五种运行模式（dry-run / fill-metadata-abstract / fill-abstracts / fill-tags / build-graph） |
| `file_extract.py` | 多格式文本提取（PDF / Word / Excel / PowerPoint / HTML / Markdown / TXT / CSV / JSON / RTF / EPUB / ODT）；图片缩放与 base64 编码 |
| `llm_client.py` | LLM 调用（含 retry）、JSON 解析（含控制字符修复）、Prompt 构建、VL 图片识别 |
| `zotero_api.py` | Zotero HTTP API 交互、元数据工具函数 |
| `zotero_db.py` | Zotero SQLite 操作（reparent、apply abstracts、tag cleanup） |
| `zotero_process.py` | Zotero 进程管理（检测、关闭、重启） |
| `fill_tags.py` | 无标签条目扫描、全文提取、LLM 标签生成 |
| `graph_builder.py` | 知识图谱构建（读取 SQLite、同义词归一化、建边、度数过滤、Leiden 聚类、JSON/HTML/报告导出） |
| `graph_visualize.py` | 从 `graph/graph.json` 生成交互式 HTML（Pyvis，按社区着色） |

## 项目结构

```
zotero_llm_metadata/
├── __main__.py         # 入口 + 五种运行模式
├── config.py           # 参数集中管理（模型、路径、图谱参数等）
├── runners.py          # 各模式的顶层调度逻辑
│
├── file_extract.py     # 多格式文本提取 + 图片处理
│   ├── detect_file_type
│   ├── extract_pdf_text / extract_word_text / extract_excel_text
│   ├── extract_pptx_text / extract_html_text / extract_markdown_text
│   ├── extract_txt_text / extract_csv_text / extract_json_text
│   ├── extract_rtf_text / extract_epub_text / extract_odt_text
│   ├── resize_and_encode_image
│   └── read_file_url
│
├── llm_client.py       # LLM / VL 调用、重试、Prompt 构建
│   ├── request_llm_with_retry
│   ├── extract_json / _sanitize_json_control_chars
│   ├── build_prompt / build_abstract_prompt / build_image_extract_prompt
│   ├── generate_abstract_for_item
│   └── extract_text_from_image
│
├── zotero_api.py       # Zotero HTTP API 交互
│   ├── fetch_no_metadata_items / fetch_no_abstract_items
│   ├── get_child_attachment_keys / get_local_item_data
│   ├── find_local_item_by_tag / get_inherited_collections
│   └── normalize_tags / build_item_data / format_creators
│
├── zotero_db.py        # Zotero SQLite 操作
│   ├── reparent_attachments_in_db
│   ├── apply_abstracts_from_mappings
│   ├── load_repair_mappings_from_jsonl / load_abstract_mappings_from_jsonl
│   └── cleanup_llm_tags
│
├── zotero_process.py   # Zotero 进程管理
│   ├── is_zotero_running
│   ├── close_zotero / reopen_zotero
│   └── ensure_zotero_closed
│
├── fill_tags.py        # 无标签条目扫描 + LLM 标签生成
│   ├── fetch_no_tag_items
│   └── generate_tags_for_item
│
├── graph_builder.py    # 知识图谱构建
│   ├── SYNONYM_MAP / resolve_synonym  # 双语 + 中文近义词归一化
│   ├── TagFilter                      # 高频标签过滤
│   ├── build_graph                    # NetworkX 图构建
│   ├── graph_stats / cluster          # 统计 + Leiden 聚类
│   └── to_json / to_html / generate_report  # 导出
│
├── graph_visualize.py  # 从 graph.json 生成交互式 HTML（Pyvis）
│
└── graph/              # --build-graph 输出目录
    ├── graph.json
    ├── GRAPH_REPORT.md
    ├── TAG_FILTER.md
    └── graph_vis.html
```
