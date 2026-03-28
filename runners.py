import json
import random
import sys
import time
from types import SimpleNamespace
from urllib.parse import quote

import httpx

from config import LLM_RETRIES, LLM_BACKOFF, LLM_MAX_BACKOFF
from file_extract import (
    detect_file_type,
    extract_csv_text, extract_excel_text, extract_epub_text, extract_html_text,
    extract_json_text, extract_markdown_text, extract_odt_text,
    extract_pdf_text, extract_pptx_text, extract_rtf_text,
    extract_txt_text, extract_word_text,
    read_file_url, resize_and_encode_image, truncate_for_print, truncate_to_token_limit,
)
from llm_client import (
    build_abstract_prompt, build_prompt, build_evidence_text,
    extract_json, extract_text_from_image,
    generate_abstract_for_item, request_llm_with_retry,
)
from zotero_api import (
    build_item_data, fetch_no_abstract_items, fetch_no_metadata_items,
    find_local_item_by_tag, get_child_attachment_keys,
    get_inherited_collections, get_local_item_data, join_url, normalize_tags,
)
from zotero_db import (
    apply_abstracts_from_mappings, cleanup_llm_tags,
    load_abstract_mappings_from_jsonl, load_repair_mappings_from_jsonl,
    reparent_attachments_in_db,
)
from zotero_process import ensure_zotero_closed, is_zotero_running, reopen_zotero


# ---------------------------------------------------------------------------
# Mode: --dry-run
# ---------------------------------------------------------------------------

def run_dry_run(args: SimpleNamespace, client: httpx.Client) -> None:
    items = fetch_no_metadata_items(
        client=client,
        base=args.base,
        list_path=args.no_meta_list_path,
        timeout=args.timeout,
        limit=args.limit,
        page_size=args.no_meta_page_size,
        max_pages=args.no_meta_max_pages,
    )
    if items:
        print(f"[DRY RUN] {len(items)} item(s) without metadata:")
        for i, (filename, key) in enumerate(items, 1):
            print(f"  {i:3}. [{key}] {filename}")
    else:
        print("[DRY RUN] No standalone no-metadata attachments found.")

    try:
        no_abstract = fetch_no_abstract_items(
            client=client,
            base=args.base,
            library_id=args.fill_abstracts_library_id,
            endpoint=args.fill_abstracts_endpoint,
            timeout=args.timeout,
            limit=args.fill_abstracts_limit,
            page_size=args.fill_abstracts_page_size,
            max_pages=args.fill_abstracts_max_pages,
            include_attachments=args.fill_abstracts_include_attachments,
        )
    except Exception as e:
        print(f"[DRY RUN] no-abstract scan failed: {e}", file=sys.stderr)
        return

    if no_abstract:
        print(f"\n[DRY RUN] {len(no_abstract)} item(s) without abstractNote:")
        for i, row in enumerate(no_abstract, 1):
            print(f"  {i:3}. [{row['key']}] ({row['itemType']}) {row['title'] or '(no title)'}")
    else:
        print("\n[DRY RUN] No items without abstractNote found.")


# ---------------------------------------------------------------------------
# Shared helper: attachment text extraction
# ---------------------------------------------------------------------------

def _extract_text_from_attachment(
    client: httpx.Client,
    base: str,
    library_id: str,
    akey: str,
    timeout: int,
    max_pages: int,
    max_tokens: int,
    filename: str = "",
    vl_model: str = "",
    image_max_long_side: int = 1280,
    llm_base: str = "",
    api_key: str = "",
) -> tuple[str, str, int, bool, bool]:
    """Download attachment file and extract text.
    Returns (text, file_type, total_pages, truncated_pages, truncated_tokens).
    Returns ('', '', 0, False, False) on failure.
    If filename is not provided, it is fetched from the item metadata API.
    Images require vl_model/llm_base/api_key to be set.
    """
    if not filename:
        item_meta = get_local_item_data(client=client, base=base, library_id=library_id,
                                        item_key=akey, timeout=timeout)
        filename = str(item_meta.get("filename", item_meta.get("title", ""))).strip()

    url = join_url(base, f"users/{library_id}/items/{quote(akey)}/file")
    try:
        r = client.get(url, timeout=timeout, follow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            location = r.headers.get("Location", "")
            if location.startswith("file://"):
                file_bytes = read_file_url(location)
                content_type = r.headers.get("Content-Type", "")
            else:
                print(f"  SKIP {akey}: redirect to unsupported location {location}", file=sys.stderr)
                return "", "", 0, False, False
        elif r.status_code >= 400:
            print(f"  SKIP {akey}: {r.status_code}", file=sys.stderr)
            return "", "", 0, False, False
        else:
            if r.text.startswith("file://"):
                file_bytes = read_file_url(r.text.strip())
                content_type = r.headers.get("Content-Type", "")
            else:
                file_bytes = r.content
                content_type = r.headers.get("Content-Type", "")
    except Exception as e:
        print(f"  SKIP {akey}: download error: {e}", file=sys.stderr)
        return "", "", 0, False, False

    file_type = detect_file_type(filename, content_type)
    if not file_type:
        print(f"  SKIP {akey}: unsupported file type (ct={content_type}, name={filename})", file=sys.stderr)
        return "", "", 0, False, False

    if file_type == "image":
        if not vl_model or not llm_base or not api_key:
            print(f"  SKIP {akey}: image file but vl_model/llm_base/api_key not configured", file=sys.stderr)
            return "", "", 0, False, False
        try:
            image_b64, mime_type = resize_and_encode_image(file_bytes, image_max_long_side)
        except Exception as e:
            print(f"  SKIP {akey}: image resize error: {e}", file=sys.stderr)
            return "", "", 0, False, False
        try:
            text = extract_text_from_image(
                client=client,
                image_b64=image_b64,
                mime_type=mime_type,
                llm_base=llm_base,
                vl_model=vl_model,
                api_key=api_key,
                timeout=timeout,
                retries=LLM_RETRIES,
                backoff=LLM_BACKOFF,
                max_backoff=LLM_MAX_BACKOFF,
                debug=False,
            )
        except Exception as e:
            print(f"  SKIP {akey}: image VL error: {e}", file=sys.stderr)
            return "", "", 0, False, False
        return text, "image", 1, False, False

    try:
        if file_type == "pdf":
            text, total_pages, truncated_pages = extract_pdf_text(file_bytes, max_pages)
        elif file_type == "word":
            text, total_pages, truncated_pages = extract_word_text(file_bytes)
        elif file_type == "pptx":
            text, total_pages, truncated_pages = extract_pptx_text(file_bytes)
        elif file_type == "excel":
            text, total_pages, truncated_pages = extract_excel_text(file_bytes)
        elif file_type == "html":
            text, total_pages, truncated_pages = extract_html_text(file_bytes)
        elif file_type == "markdown":
            text, total_pages, truncated_pages = extract_markdown_text(file_bytes)
        elif file_type == "txt":
            text, total_pages, truncated_pages = extract_txt_text(file_bytes)
        elif file_type == "csv":
            text, total_pages, truncated_pages = extract_csv_text(file_bytes)
        elif file_type == "json":
            text, total_pages, truncated_pages = extract_json_text(file_bytes)
        elif file_type == "rtf":
            text, total_pages, truncated_pages = extract_rtf_text(file_bytes)
        elif file_type == "epub":
            text, total_pages, truncated_pages = extract_epub_text(file_bytes)
        elif file_type == "odt":
            text, total_pages, truncated_pages = extract_odt_text(file_bytes)
        else:
            text, total_pages, truncated_pages = "", 0, False
    except Exception as e:
        print(f"  SKIP {akey}: parse error: {e}", file=sys.stderr)
        return "", "", 0, False, False

    text, truncated_tokens = truncate_to_token_limit(text, max_tokens)
    return text, file_type, total_pages, truncated_pages, truncated_tokens


# ---------------------------------------------------------------------------
# Mode: --fill-abstracts
# ---------------------------------------------------------------------------

def run_fill_abstracts(args: SimpleNamespace, client: httpx.Client) -> None:
    try:
        rows = fetch_no_abstract_items(
            client=client,
            base=args.base,
            library_id=args.fill_abstracts_library_id,
            endpoint=args.fill_abstracts_endpoint,
            timeout=args.timeout,
            limit=args.fill_abstracts_limit,
            page_size=args.fill_abstracts_page_size,
            max_pages=args.fill_abstracts_max_pages,
            include_attachments=args.fill_abstracts_include_attachments,
        )
    except Exception as e:
        print(f"fill-abstracts scan failed: {e}", file=sys.stderr)
        sys.exit(2)

    if not rows:
        print("fill-abstracts: no items with missing abstractNote found.", file=sys.stderr)
        return

    print(f"fill-abstracts: found {len(rows)} item(s) with missing abstractNote.")
    with open(args.fill_abstracts_out, "w", encoding="utf-8") as fa_out:
        for i, row in enumerate(rows, start=1):
            key = str(row.get("key", "")).strip()
            if not key:
                row["llm_status"] = "skip_no_key"
                fa_out.write(json.dumps(row, ensure_ascii=False) + "\n")
                continue

            item_data = get_local_item_data(
                client=client, base=args.base,
                library_id=args.fill_abstracts_library_id,
                item_key=key, timeout=args.timeout,
            )
            if not item_data:
                row["llm_status"] = "error_get_item"
                row["llm_error"] = "empty response"
                fa_out.write(json.dumps(row, ensure_ascii=False) + "\n")
                continue

            row["itemType"] = str(item_data.get("itemType", row.get("itemType", "")))
            row["title"] = str(item_data.get("title", row.get("title", ""))).strip()
            row["date"] = str(item_data.get("date", row.get("date", ""))).strip()

            _vl_kwargs = dict(
                vl_model=args.vl_model,
                image_max_long_side=args.image_max_long_side,
                llm_base=args.llm_base,
                api_key=args.api_key,
            )
            fulltext = ""
            if str(item_data.get("itemType", "")) == "attachment":
                fulltext, *_ = _extract_text_from_attachment(
                    client=client, base=args.base,
                    library_id=args.fill_abstracts_library_id,
                    akey=key, timeout=args.timeout,
                    max_pages=args.max_pages,
                    max_tokens=args.max_tokens,
                    **_vl_kwargs,
                )
            else:
                for akey in get_child_attachment_keys(
                    client=client, base=args.base,
                    library_id=args.fill_abstracts_library_id,
                    item_key=key, timeout=args.timeout,
                ):
                    text, *_ = _extract_text_from_attachment(
                        client=client, base=args.base,
                        library_id=args.fill_abstracts_library_id,
                        akey=akey, timeout=args.timeout,
                        max_pages=args.max_pages,
                        max_tokens=args.max_tokens,
                        **_vl_kwargs,
                    )
                    if text.strip():
                        fulltext = text
                        break

            evidence_text = build_evidence_text(
                item_data=item_data,
                attachment_fulltext=fulltext,
                max_fulltext_chars=args.fill_abstracts_max_fulltext_chars,
            )
            if args.print_prompt:
                fa_system, fa_user = build_abstract_prompt(key, evidence_text)
                print(f"\n=== PROMPT {key} (system) ===\n{truncate_for_print(fa_system, args.print_max_chars)}", file=sys.stderr)
                print(f"\n=== PROMPT {key} (user) ===\n{truncate_for_print(fa_user, args.print_max_chars)}", file=sys.stderr)

            try:
                llm = generate_abstract_for_item(
                    client=client,
                    item_key=key,
                    evidence_text=evidence_text,
                    llm_base=args.llm_base,
                    model=args.model,
                    api_key=args.api_key,
                    timeout=args.timeout,
                    retries=LLM_RETRIES,
                    backoff=LLM_BACKOFF,
                    max_backoff=LLM_MAX_BACKOFF,
                    max_output_tokens=args.fill_abstracts_max_output_tokens,
                    debug=args.print_response,
                )
            except Exception as e:
                row["llm_status"] = "error_llm"
                row["llm_error"] = str(e)
                fa_out.write(json.dumps(row, ensure_ascii=False) + "\n")
                continue

            if args.print_response:
                print(f"\n=== RESPONSE {key} ===\n{truncate_for_print(llm.get('raw_response', ''), args.print_max_chars)}", file=sys.stderr)

            abstract = str(llm.get("abstractNote", "")).strip()
            row["llm_confidence"] = llm.get("confidence", 0)
            row["llm_insufficient_evidence"] = bool(llm.get("insufficient_evidence", False))
            row["llm_status"] = "generated" if abstract else "empty"
            row["llm_raw_response"] = llm.get("raw_response", "")
            if abstract:
                row["abstractNote"] = abstract

            fa_out.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"fill-abstracts [{i}/{len(rows)}] {key}: {row['llm_status']}")

            if args.fill_abstracts_sleep_secs > 0 and i < len(rows):
                time.sleep(args.fill_abstracts_sleep_secs)

    print(f"fill-abstracts done. Output: {args.fill_abstracts_out}")

    if not args.db_path:
        print("fill-abstracts: db_path 未配置，跳过自动写入。", file=sys.stderr)
        return
    zotero_was_running = is_zotero_running()
    if not ensure_zotero_closed("fill-abstracts"):
        return
    try:
        mappings, info = load_abstract_mappings_from_jsonl(args.fill_abstracts_out)
    except Exception as e:
        print(f"fill-abstracts: 无法读取摘要映射: {e}", file=sys.stderr)
        if zotero_was_running:
            reopen_zotero()
        return
    print(
        "apply-abstracts mapping summary: "
        f"total_lines={info['total_lines']} parsed={info['parsed_lines']} "
        f"valid={info['valid_mappings']} invalid_json={info['invalid_lines']} "
        f"missing_keys={info['missing_keys']}"
    )
    if mappings:
        stats = apply_abstracts_from_mappings(args.db_path, mappings)
        print(f"apply-abstracts done: written={stats['written']} failed={stats['failed']}")
        for e in stats["errors"]:
            print(e, file=sys.stderr)
    else:
        print("apply-abstracts skipped: no valid mappings found.", file=sys.stderr)
    if zotero_was_running:
        reopen_zotero()


# ---------------------------------------------------------------------------
# Mode: normal (metadata extraction)
# ---------------------------------------------------------------------------

def run_extract_metadata(args: SimpleNamespace, client: httpx.Client) -> None:
    items = fetch_no_metadata_items(
        client=client,
        base=args.base,
        list_path=args.no_meta_list_path,
        timeout=args.timeout,
        limit=args.limit,
        page_size=args.no_meta_page_size,
        max_pages=args.no_meta_max_pages,
    )
    if not items:
        print("No standalone no-metadata attachments found from API.", file=sys.stderr)
        return

    if args.write_mode not in ("connector", "none"):
        print(f"Invalid WRITE_MODE: {args.write_mode}. Use connector/none.", file=sys.stderr)
        sys.exit(2)

    mappings = []
    with open(args.out, "w", encoding="utf-8") as out_f:
        for filename, key in items:
            text, _, total_pages, truncated_pages, truncated_tokens = _extract_text_from_attachment(
                client=client, base=args.base, library_id="0",
                akey=key, timeout=args.timeout,
                max_pages=args.max_pages, max_tokens=args.max_tokens,
                filename=filename,
                vl_model=args.vl_model,
                image_max_long_side=args.image_max_long_side,
                llm_base=args.llm_base,
                api_key=args.api_key,
            )
            if not text:
                continue
            system, user = build_prompt(filename, text)
            if args.print_prompt:
                print(f"\n=== PROMPT {key} (system) ===\n{truncate_for_print(system, args.print_max_chars)}", file=sys.stderr)
                print(f"\n=== PROMPT {key} (user) ===\n{truncate_for_print(user, args.print_max_chars)}", file=sys.stderr)

            llm_payload = {
                "model": args.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.2,
                "max_tokens": args.max_output_tokens,
            }

            did_llm_request = False
            try:
                content = request_llm_with_retry(
                    client,
                    join_url(args.llm_base, "chat/completions"),
                    headers={
                        "Authorization": f"Bearer {args.api_key}",
                        "Content-Type": "application/json",
                    },
                    payload=llm_payload,
                    timeout=args.timeout,
                    retries=LLM_RETRIES,
                    backoff=LLM_BACKOFF,
                    max_backoff=LLM_MAX_BACKOFF,
                    key=key,
                    debug=args.print_response,
                    max_print_chars=args.print_max_chars,
                )
                did_llm_request = True
            except Exception as e:
                print(f"LLM FAIL {key}: {e}", file=sys.stderr)
                continue
            finally:
                if did_llm_request:
                    sleep_secs = random.uniform(30, 45)
                    print(f"Sleeping {sleep_secs:.1f}s after LLM request...", file=sys.stderr)
                    time.sleep(sleep_secs)

            if args.print_response:
                print(f"\n=== RESPONSE {key} ===\n{truncate_for_print(content, args.print_max_chars)}", file=sys.stderr)

            json_text = extract_json(content)
            metadata = None
            if json_text:
                try:
                    metadata = json.loads(json_text)
                except Exception:
                    metadata = None

            record = {
                "item_key": key,
                "filename": filename,
                "total_pages": total_pages,
                "truncated_pages": truncated_pages,
                "truncated_tokens": truncated_tokens,
                "metadata": metadata,
                "raw_response": content,
            }

            if args.write_mode == "connector" and metadata:
                write_status = {"created_parent": False, "parent_key": "", "error": ""}
                try:
                    tag_marker = f"__llm_import__{key}"
                    item_data = build_item_data(metadata)
                    if not item_data.get("itemType"):
                        item_data["itemType"] = "document"
                    inherited_collections = get_inherited_collections(
                        client, args.base, "0", key, timeout=args.timeout
                    )
                    if inherited_collections:
                        item_data["collections"] = inherited_collections
                    tags = normalize_tags(item_data.get("tags", []))
                    tags.append({"tag": tag_marker})
                    item_data["tags"] = tags
                    parent_key = find_local_item_by_tag(client, args.base, "0", tag_marker, timeout=args.timeout)
                    if not parent_key:
                        payload = {"items": [item_data], "uri": "about:blank"}
                        resp = client.post(
                            args.connector_url,
                            headers={"Content-Type": "application/json"},
                            json=payload,
                            timeout=args.timeout,
                        )
                        if resp.status_code != 201:
                            write_status["error"] = f"Connector failed: {resp.status_code} {resp.text}"
                        else:
                            parent_key = find_local_item_by_tag(client, args.base, "0", tag_marker, timeout=args.timeout)
                    if parent_key:
                        write_status["created_parent"] = True
                        write_status["parent_key"] = parent_key
                        mappings.append({"attachment_key": key, "parent_key": parent_key, "tag": tag_marker})
                    elif not write_status["error"]:
                        write_status["error"] = "Created item but could not find parent key by tag."
                except Exception as e:
                    write_status["error"] = str(e)
                record["write_status"] = write_status

            out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"OK {key} -> {args.out}")

    if mappings:
        if not args.db_path:
            print("Reparent skipped: db_path is not set.", file=sys.stderr)
        else:
            try:
                stats = reparent_attachments_in_db(args.db_path, mappings, cleanup_tag=args.cleanup_tag)
                print(f"Reparented attachments: moved={stats['moved']} failed={stats['failed']}")
                for e in stats["errors"]:
                    print(e, file=sys.stderr)
            except Exception as e:
                print(f"Reparent failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Mode: --fill-metadata-abstract  (元数据提取 + repair 合并模式)
# ---------------------------------------------------------------------------

def run_fill_metadata_abstract(args: SimpleNamespace, client: httpx.Client) -> None:
    """① 提取元数据（Connector 写入，Zotero 需运行）
       ② 关闭 Zotero
       ③ repair: 从 metadata.jsonl 修复未完成的 reparent + 清理标签
       ④ 重启 Zotero
    """
    # 第一步：元数据提取（内部已对本次新增条目做 reparent）
    run_extract_metadata(args, client)

    if not args.db_path:
        print("fill-metadata-abstract: db_path 未配置，跳过 repair 步骤。", file=sys.stderr)
        return

    # 第二步：关闭 Zotero，对历史 jsonl 做全量 repair（修复之前失败的 reparent）
    zotero_was_running = is_zotero_running()
    if not ensure_zotero_closed("fill-metadata-abstract"):
        return
    try:
        mappings, info = load_repair_mappings_from_jsonl(args.out)
    except Exception as e:
        print(f"fill-metadata-abstract: 无法读取 repair 映射: {e}", file=sys.stderr)
        if zotero_was_running:
            reopen_zotero()
        return
    print(
        "repair mapping summary: "
        f"total_lines={info['total_lines']} parsed={info['parsed_lines']} "
        f"valid={info['valid_mappings']} invalid_json={info['invalid_lines']} "
        f"missing_keys={info['missing_keys']}"
    )
    if mappings:
        try:
            stats = reparent_attachments_in_db(args.db_path, mappings, cleanup_tag=args.cleanup_tag)
            print(f"repair reparent done: moved={stats['moved']} failed={stats['failed']}")
            for e in stats["errors"]:
                print(e, file=sys.stderr)
        except Exception as e:
            print(f"repair reparent failed: {e}", file=sys.stderr)
    try:
        tag_stats = cleanup_llm_tags(args.db_path)
        print(
            f"tag cleanup done: found={tag_stats['tags_found']} "
            f"item_tags_deleted={tag_stats['item_tags_deleted']} "
            f"tags_deleted={tag_stats['tags_deleted']}"
        )
    except Exception as e:
        print(f"tag cleanup failed: {e}", file=sys.stderr)
    if zotero_was_running:
        reopen_zotero()
