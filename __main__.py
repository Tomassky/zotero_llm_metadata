#!/usr/bin/env python3
import argparse
import sys

try:
    import httpx
except Exception:
    print("Missing dependency: httpx. Install with `python3 -m pip install httpx`.", file=sys.stderr)
    raise

from config import make_args
from runners import run_dry_run, run_fill_abstracts, run_fill_metadata_abstract, run_build_graph, run_fill_tags


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Zotero LLM metadata extractor — 用 LLM 为 Zotero 条目提取元数据或生成摘要。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
运行模式说明
============

  --fill-metadata-abstract  全流程元数据模式（推荐，需 Zotero 运行）
                          ① 扫描缺少元数据的独立附件，提取文本/图片，调用 LLM 生成
                             结构化元数据，通过 Connector 写入 Zotero，结果存入 metadata.jsonl
                          ② 自动关闭 Zotero
                          ③ 从 metadata.jsonl 修复未完成的附件挂载，清理 LLM 标签
                          ④ 自动重启 Zotero
                          支持格式：.pdf / .docx / .doc / .docm / .xlsx / .xls / .pptx / .ppt / .pptm
                                    .html / .htm / .md / .txt / .csv / .json / .rtf / .epub / .odt
                                    .png / .jpg / .jpeg / .gif / .webp（图片需配置 vl_model）

  --fill-abstracts        全流程摘要模式（推荐，需 Zotero 运行）
                          ① 扫描缺少 abstractNote 的条目，读取附件全文，调用 LLM 生成
                             中文摘要（≥500 字），结果保存到 fill_abstracts.jsonl
                          ② 自动关闭 Zotero
                          ③ 将摘要写入 Zotero SQLite 数据库
                          ④ 自动重启 Zotero

  --dry-run               预览模式（不调用 LLM）
                          列出当前缺少元数据的附件，以及缺少 abstractNote 的条目。

  --build-graph           知识图构建模式（不需要 Zotero 运行）
                          从 Zotero SQLite 读取文献、tag、collection 数据，
                          构建 NetworkX 知识图，运行 Leiden/Louvain 社区检测，
                          导出 graph.json、GRAPH_REPORT.md、TAG_FILTER.md。

  --fill-tags             标签生成模式（需 Zotero 运行）
                          ① 扫描没有 tag 的条目，读取附件全文，调用 LLM 生成标签
                          ② 自动关闭 Zotero
                          ③ 将标签写入 Zotero SQLite 数据库
                          ④ 自动重启 Zotero

环境变量
========
  DASHSCOPE_API_KEY       调用 LLM 所需的 API Key
""",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fill-metadata-abstract", action="store_true",
                      help="全流程元数据模式：提取元数据 + 自动 repair")
    mode.add_argument("--fill-abstracts", action="store_true",
                      help="全流程摘要模式：生成摘要 + 自动写入数据库")
    mode.add_argument("--fill-tags", action="store_true",
                      help="标签生成模式：为无 tag 条目批量生成标签 + 写入数据库")
    mode.add_argument("--build-graph", action="store_true",
                      help="构建知识图：从 Zotero DB 建图 + Leiden 社区检测 + 导出")
    mode.add_argument("--dry-run", action="store_true",
                      help="预览缺少元数据和摘要的条目列表，不调用 LLM")
    parsed = parser.parse_args()

    if not any([parsed.fill_metadata_abstract, parsed.fill_abstracts, parsed.dry_run, parsed.build_graph, parsed.fill_tags]):
        parser.print_help()
        sys.exit(0)

    args = make_args(parsed)

    if args.build_graph:
        run_build_graph(args)
        return

    needs_llm = not args.dry_run and not args.fill_tags
    if needs_llm and not args.api_key:
        print(
            "Missing API key. Set the DASHSCOPE_API_KEY environment variable.",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.fill_tags and not args.api_key:
        print(
            "Missing API key. Set the DASHSCOPE_API_KEY environment variable.",
            file=sys.stderr,
        )
        sys.exit(2)

    with httpx.Client(follow_redirects=False) as client:
        if args.dry_run:
            run_dry_run(args, client)
        elif args.fill_abstracts:
            run_fill_abstracts(args, client)
        elif args.fill_tags:
            run_fill_tags(args, client)
        else:
            run_fill_metadata_abstract(args, client)


if __name__ == "__main__":
    main()
