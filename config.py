import argparse
import os
from types import SimpleNamespace


# ---------------------------------------------------------------------------
# LLM retry constants
# ---------------------------------------------------------------------------

LLM_RETRIES = 4
LLM_BACKOFF = 5.0
LLM_MAX_BACKOFF = 60.0


# ---------------------------------------------------------------------------
# Shared config
# ---------------------------------------------------------------------------

def make_args(parsed: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        base="http://localhost:23119/api/",
        no_meta_list_path="users/0/items",
        no_meta_page_size=100,
        no_meta_max_pages=1000,
        dry_run=parsed.dry_run,
        limit=30,
        max_pages=15,
        max_tokens=20000,
        llm_base="https://coding.dashscope.aliyuncs.com/v1",
        model="qwen3.6-plus",
        api_key=os.getenv("DASHSCOPE_API_KEY", ""),
        out="metadata.jsonl",
        max_output_tokens=1200,
        write_mode="connector",
        connector_url="http://127.0.0.1:23119/connector/saveItems",
        db_path=os.path.expanduser("~/Nextcloud/Zotero/zotero.sqlite"),
        cleanup_tag=False,
        print_prompt=True,
        print_response=True,
        print_max_chars=4000,
        timeout=180,
        fill_metadata_abstract=parsed.fill_metadata_abstract,
        fill_abstracts=parsed.fill_abstracts,
        fill_abstracts_library_id="0",
        fill_abstracts_endpoint="items/top",
        fill_abstracts_limit=200,
        fill_abstracts_page_size=100,
        fill_abstracts_max_pages=1000,
        fill_abstracts_include_attachments=True,
        fill_abstracts_max_fulltext_chars=12000,
        fill_abstracts_sleep_secs=0.0,
        fill_abstracts_out="fill_abstracts.jsonl",
        fill_abstracts_max_output_tokens=2000,
        vl_model="qwen3-vl-flash",
        image_max_long_side=1280,
        # --build-graph settings
        build_graph=parsed.build_graph,
        graph_output_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)), "graph"),
        max_degree=15,
        tag_fraction=0.20,
        # --fill-tags settings
        fill_tags=parsed.fill_tags,
        fill_tags_out="fill_tags.jsonl",
        fill_tags_max_fulltext_chars=8000,
        fill_tags_max_output_tokens=500,
        zotero_storage_dir=os.path.expanduser("~/Nextcloud/Zotero/storage"),
    )
