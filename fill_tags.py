"""
为没有 tag 的 Zotero 条目批量生成标签。

流程：
  1. 从 Zotero SQLite 扫描无 tag 条目及其子附件
  2. 读取附件全文（直接从 storage 目录，支持全格式）
  3. 调用 LLM 生成 tag（结构化 JSON 输出）
  4. 结果存入 fill_tags.jsonl

用法：
  python3 __main__.py --fill-tags
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

from file_extract import (
    detect_file_type,
    extract_csv_text, extract_excel_text, extract_epub_text, extract_html_text,
    extract_json_text, extract_markdown_text, extract_odt_text,
    extract_pdf_text, extract_pptx_text, extract_rtf_text,
    extract_txt_text, extract_word_text,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 扫描无 tag 条目
# ---------------------------------------------------------------------------

def fetch_no_tag_items(db_path: str) -> list[dict[str, Any]]:
    """从 Zotero SQLite 读取没有 tag 的条目，包含附件信息。

    Returns:
        List of item dicts, each with:
          - key, title, abstract, url, item_type
          - attachments: list of {"att_key", "content_type", "path", "filename"}
    """
    uri = f"file:{db_path}?immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row

    # 1. Get items with NO tags
    items_query = """
    SELECT
        i.itemID,
        i.key,
        it.typeName as item_type,
        title_val.value as title,
        abstract_val.value as abstract,
        url_val.value as url
    FROM items i
    JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
    LEFT JOIN itemData id_title ON i.itemID = id_title.itemID
        AND id_title.fieldID = (SELECT fieldID FROM fields WHERE fieldName = 'title')
    LEFT JOIN itemDataValues title_val ON id_title.valueID = title_val.valueID
    LEFT JOIN itemData id_abs ON i.itemID = id_abs.itemID
        AND id_abs.fieldID = (SELECT fieldID FROM fields WHERE fieldName = 'abstractNote')
    LEFT JOIN itemDataValues abstract_val ON id_abs.valueID = abstract_val.valueID
    LEFT JOIN itemData id_url ON i.itemID = id_url.itemID
        AND id_url.fieldID = (SELECT fieldID FROM fields WHERE fieldName = 'url')
    LEFT JOIN itemDataValues url_val ON id_url.valueID = url_val.valueID
    LEFT JOIN itemTags itag ON i.itemID = itag.itemID
    WHERE it.typeName NOT IN ('attachment', 'note', 'annotation')
      AND itag.tagID IS NULL
    ORDER BY i.key
    """
    items_rows = conn.execute(items_query).fetchall()

    # 2. Get child attachments for these items
    item_ids = [row["itemID"] for row in items_rows]
    if not item_ids:
        conn.close()
        return []

    placeholders = ",".join("?" * len(item_ids))
    att_query = f"""
    SELECT
        ia.itemID as att_itemID,
        att.key as att_key,
        ia.parentItemID,
        ia.contentType,
        ia.path
    FROM itemAttachments ia
    JOIN items att ON ia.itemID = att.itemID
    WHERE ia.parentItemID IN ({placeholders})
      AND ia.contentType IS NOT NULL
    ORDER BY ia.parentItemID, ia.itemID
    """
    att_rows = conn.execute(att_query, item_ids).fetchall()

    # Build attachment lookup: parentItemID -> [attachment dicts]
    att_by_parent: dict[int, list[dict]] = {}
    for ar in att_rows:
        raw_path = ar["path"] or ""
        filename = raw_path.replace("storage:", "") if raw_path.startswith("storage:") else raw_path
        att_by_parent.setdefault(ar["parentItemID"], []).append({
            "att_key": ar["att_key"],
            "content_type": ar["contentType"] or "",
            "path": raw_path,
            "filename": filename,
        })

    # 3. Assemble results
    result = []
    for row in items_rows:
        item_id = row["itemID"]
        attachments = att_by_parent.get(item_id, [])
        # Filter: prefer HTML snapshots over other types for webpage items
        result.append({
            "key": row["key"],
            "title": row["title"] or row["key"],
            "abstract": row["abstract"] or "",
            "url": row["url"] or "",
            "item_type": row["item_type"],
            "attachments": attachments,
        })

    conn.close()
    return result


# ---------------------------------------------------------------------------
# 读取附件全文
# ---------------------------------------------------------------------------

def read_attachment_text(
    zotero_storage_dir: str,
    attachment: dict[str, Any],
) -> tuple[str, str]:
    """读取 Zotero storage 中的附件文件内容。

    Args:
        zotero_storage_dir: e.g. "/Users/tomas/Nextcloud/Zotero/storage"
        attachment: dict with att_key, content_type, filename

    Returns:
        (text, file_type) or ("", "") on failure.
    """
    att_key = attachment.get("att_key", "")
    filename = attachment.get("filename", "")
    content_type = attachment.get("content_type", "")

    if not att_key or not filename:
        return "", ""

    filepath = os.path.join(zotero_storage_dir, att_key, filename)
    if not os.path.exists(filepath):
        logger.debug(f"Attachment file not found: {filepath}")
        return "", ""

    try:
        file_bytes = open(filepath, "rb").read()
    except Exception as e:
        logger.debug(f"Cannot read attachment {filepath}: {e}")
        return "", ""

    file_type = detect_file_type(filename, content_type)
    if not file_type:
        logger.debug(f"Unsupported attachment type: ct={content_type}, name={filename}")
        return "", ""

    try:
        if file_type == "pdf":
            text, _, _ = extract_pdf_text(file_bytes, max_pages=50)
        elif file_type == "word":
            text, _, _ = extract_word_text(file_bytes)
        elif file_type == "pptx":
            text, _, _ = extract_pptx_text(file_bytes)
        elif file_type == "excel":
            text, _, _ = extract_excel_text(file_bytes)
        elif file_type == "html":
            text, _, _ = extract_html_text(file_bytes)
        elif file_type == "markdown":
            text, _, _ = extract_markdown_text(file_bytes)
        elif file_type == "txt":
            text, _, _ = extract_txt_text(file_bytes)
        elif file_type == "csv":
            text, _, _ = extract_csv_text(file_bytes)
        elif file_type == "json":
            text, _, _ = extract_json_text(file_bytes)
        elif file_type == "rtf":
            text, _, _ = extract_rtf_text(file_bytes)
        elif file_type == "epub":
            text, _, _ = extract_epub_text(file_bytes)
        elif file_type == "odt":
            text, _, _ = extract_odt_text(file_bytes)
        else:
            return "", file_type
    except Exception as e:
        logger.debug(f"Parse error for {filepath}: {e}")
        return "", ""

    return text, file_type


def get_best_attachment_text(
    zotero_storage_dir: str,
    item: dict[str, Any],
    max_chars: int = 8000,
) -> tuple[str, str]:
    """从条目的附件中提取最佳全文。

    优先选择 HTML 快照（webpage 条目的网页快照），
    其次 PDF，最后其他格式。截断到 max_chars 以节省 token。

    Returns:
        (truncated_text, file_type) or ("", "") if no attachment content.
    """
    attachments = item.get("attachments", [])
    if not attachments:
        return "", ""

    # Prefer HTML for webpage items, then PDF, then others
    preferred_order = ["text/html", "application/pdf"]
    ordered_atts = []
    for ct in preferred_order:
        for att in attachments:
            if att.get("content_type") == ct:
                ordered_atts.append(att)
    # Add remaining
    for att in attachments:
        if att not in ordered_atts:
            ordered_atts.append(att)

    for att in ordered_atts:
        text, file_type = read_attachment_text(zotero_storage_dir, att)
        if text:
            # Truncate to max_chars
            if len(text) > max_chars:
                text = text[:max_chars] + "\n...[TRUNCATED]"
            return text, file_type

    return "", ""


# ---------------------------------------------------------------------------
# 构建证据文本
# ---------------------------------------------------------------------------

def build_evidence_for_item(
    item: dict[str, Any],
    fulltext: str,
    max_fulltext_chars: int = 8000,
) -> str:
    """构建发送给 LLM 的证据文本。

    Args:
        item: 条目 dict（含 key, title, abstract, url）
        fulltext: 附件全文（可能为空）
        max_fulltext_chars: 全文截断长度

    Returns:
        证据文本字符串
    """
    title = item.get("title", "") or item.get("key", "")
    abstract = item.get("abstract", "")
    url = item.get("url", "")

    parts = []
    parts.append(f"标题: {title}")
    if url:
        parts.append(f"URL: {url}")
    if abstract:
        parts.append(f"摘要: {abstract}")

    if fulltext:
        truncated = fulltext[:max_fulltext_chars]
        if len(fulltext) > max_fulltext_chars:
            truncated += "\n...[全文已截断]"
        parts.append(f"\n全文开始:\n<<<DOCUMENT>>>\n{truncated}\n<<<END_DOCUMENT>>>")
    elif abstract:
        parts.append("\n[注意: 无法获取全文，仅提供标题和摘要作为弱证据]")
    else:
        parts.append("\n[注意: 证据极其不足，仅有标题，标签可能不准确]")

    return "\n".join(parts)