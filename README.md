# zotero_llm_metadata

通过 LLM 自动为 Zotero 中的条目提取元数据、补充摘要。

## 功能

- 扫描无元数据的独立附件（PDF / Word / Excel / HTML / 图片），调用 LLM 生成结构化元数据（标题、作者、摘要、DOI 等），通过 Connector 写入 Zotero 并自动挂载父条目
- 为已有条目补充缺失的 `abstractNote`（支持读取 PDF / Word / HTML 全文，或通过 VL 模型识别图片）
- 以上两步均自动管理 Zotero 进程（写入 SQLite 前关闭，写入完成后重启）

## 依赖

```bash
pip install httpx pypdf pdfminer.six python-docx openpyxl Pillow
```

> 除 `httpx` 外均为可选依赖，按需安装：
> - `pypdf` / `pdfminer.six`：PDF 文本提取
> - `python-docx`：Word 文本提取
> - `openpyxl`：Excel 文本提取
> - `Pillow`：图片缩放与编码（图片附件必须）

## 配置

```bash
export DASHSCOPE_API_KEY=sk-xxxx
```

其他参数（Zotero API 地址、模型名称、数据库路径、VL 模型等）在 `__main__.py` 的 `_make_args()` 中直接修改。

## 使用

```bash
# 在 zotero_llm_metadata/ 同级目录执行

# 预览：列出无元数据附件 + 缺摘要条目，不调用 LLM
python -m zotero_llm_metadata --dry-run

# 全流程元数据：提取元数据 → 写入 Zotero → 自动 repair → 重启 Zotero
python -m zotero_llm_metadata --fill-metadata-abstract

# 全流程摘要：生成 abstractNote → 关闭 Zotero → 写入数据库 → 重启 Zotero
python -m zotero_llm_metadata --fill-abstracts
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

## 输出文件

| 文件 | 内容 |
|------|------|
| `metadata.jsonl` | 每行一条元数据提取结果（含原始响应和写入状态） |
| `fill_abstracts.jsonl` | 每行一条摘要生成结果（含置信度和原始响应） |

## 模块说明

| 文件 | 包含内容 |
|------|---------|
| `__main__.py` | 入口、三种运行模式（dry-run / fill-metadata-abstract / fill-abstracts） |
| `file_extract.py` | PDF / Word / Excel / HTML 文本提取；图片缩放与 base64 编码 |
| `llm_client.py` | LLM 调用（含 retry）、JSON 解析（含控制字符修复）、Prompt 构建、VL 图片识别 |
| `zotero_api.py` | Zotero HTTP API 交互、元数据工具函数 |
| `zotero_db.py` | Zotero SQLite 操作（reparent、apply abstracts、tag cleanup） |
| `zotero_process.py` | Zotero 进程管理（检测、关闭、重启） |

## 项目结构

```
zotero_llm_metadata/
├── __main__.py         # 入口 + 三种运行模式
├── file_extract.py     # 多格式文本提取 + 图片处理
│   ├── detect_file_type
│   ├── extract_pdf_text / extract_word_text / extract_excel_text / extract_html_text
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
└── zotero_process.py   # Zotero 进程管理
    ├── is_zotero_running
    ├── close_zotero / reopen_zotero
    └── ensure_zotero_closed
```
