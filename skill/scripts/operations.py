"""
Read-only Zotero operations, extracted from the former MCP server tool bodies.

Every function returns a Markdown string and has no MCP dependency. They reuse
the connection/formatting helpers in :mod:`zotero_mcp.client` and
:mod:`zotero_mcp.utils`. The CLI (:mod:`zotero_mcp.cli`) and the Zotero skill
are the only callers.

All operations are read-only: they use the local Zotero HTTP API (pyzotero with
ZOTERO_LOCAL=true) or the read-only web API. Nothing here mutates the library.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from pathlib import Path

from client import (
    convert_to_markdown,
    format_item_metadata,
    generate_bibtex,
    get_active_library,
    get_attachment_details,
    get_zotero_client,
)
from utils import clean_html, format_creators

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _local_db_path() -> str:
    """Resolve the local Zotero SQLite path.

    ``local_db.LocalZoteroReader`` auto-detects ``~/Zotero/zotero.sqlite``, but on
    this machine the live library lives in ``~/Nextcloud/Zotero`` (matching
    ``config.py``). Honor ``ZOTERO_DB_PATH`` first, then the Nextcloud path, then
    fall back to the reader's own default.
    """
    env = os.environ.get("ZOTERO_DB_PATH")
    if env:
        return env
    nextcloud = Path.home() / "Nextcloud" / "Zotero" / "zotero.sqlite"
    if nextcloud.exists():
        return str(nextcloud)
    return str(Path.home() / "Zotero" / "zotero.sqlite")


def _coerce_limit(limit: int | str | None) -> int | None:
    if isinstance(limit, str):
        return int(limit)
    return limit


def _format_item_entry(output: list[str], index: int, item: dict, heading: str = "##") -> None:
    """Append a standard item block (title/type/key/date/authors/abstract/tags)."""
    data = item.get("data", {})
    title = data.get("title", "Untitled")
    item_type = data.get("itemType", "unknown")
    date = data.get("date", "No date")
    key = item.get("key", "")
    creators_str = format_creators(data.get("creators", []))

    output.append(f"{heading} {index}. {title}")
    output.append(f"**Type:** {item_type}")
    output.append(f"**Item Key:** {key}")
    output.append(f"**Date:** {date}")
    output.append(f"**Authors:** {creators_str}")

    if abstract := data.get("abstractNote"):
        abstract_snippet = abstract[:200] + "..." if len(abstract) > 200 else abstract
        output.append(f"**Abstract:** {abstract_snippet}")

    if tags := data.get("tags"):
        tag_list = [f"`{tag['tag']}`" for tag in tags]
        if tag_list:
            output.append(f"**Tags:** {' '.join(tag_list)}")

    output.append("")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_items(
    query: str,
    qmode: str = "titleCreatorYear",
    item_type: str = "-attachment",
    limit: int | str | None = 10,
    tag: list[str] | None = None,
) -> str:
    """Search items by query string (optionally filtered by tags)."""
    try:
        if not query.strip():
            return "Error: Search query cannot be empty"

        tag_condition_str = ""
        if tag:
            tag_condition_str = f" with tags: '{', '.join(tag)}'"
        else:
            tag = []

        zot = get_zotero_client()
        limit = _coerce_limit(limit)

        zot.add_parameters(q=query, qmode=qmode, itemType=item_type, limit=limit, tag=tag)
        results = zot.items()

        if not results:
            return f"No items found matching query: '{query}'{tag_condition_str}"

        output = [f"# Search Results for '{query}'", f"{tag_condition_str}", ""]
        for i, item in enumerate(results, 1):
            _format_item_entry(output, i, item)
        return "\n".join(output)

    except Exception as e:
        logger.error("Error searching Zotero: %s", e)
        return f"Error searching Zotero: {str(e)}"


def search_by_tag(
    tag: list[str],
    item_type: str = "-attachment",
    limit: int | str | None = 10,
) -> str:
    """Search items by tag conditions (ANDed; each supports OR and `-` exclusion)."""
    try:
        if not tag:
            return "Error: Tag cannot be empty"

        zot = get_zotero_client()
        limit = _coerce_limit(limit)

        zot.add_parameters(q="", tag=tag, itemType=item_type, limit=limit)
        results = zot.items()

        if not results:
            return f"No items found with tag: '{tag}'"

        output = [f"# Search Results for Tag: '{tag}'", ""]
        for i, item in enumerate(results, 1):
            _format_item_entry(output, i, item)
        return "\n".join(output)

    except Exception as e:
        logger.error("Error searching Zotero: %s", e)
        return f"Error searching Zotero: {str(e)}"


# ---------------------------------------------------------------------------
# Item metadata / fulltext
# ---------------------------------------------------------------------------

def get_item_metadata(
    item_key: str,
    include_abstract: bool = True,
    output_format: str = "markdown",
) -> str:
    """Get detailed metadata for an item (markdown or bibtex)."""
    try:
        zot = get_zotero_client()
        item = zot.item(item_key)
        if not item:
            return f"No item found with key: {item_key}"

        if output_format == "bibtex":
            return generate_bibtex(item)
        return format_item_metadata(item, include_abstract)

    except Exception as e:
        logger.error("Error fetching item metadata: %s", e)
        return f"Error fetching item metadata: {str(e)}"


def get_item_fulltext(item_key: str) -> str:
    """Get full text of an item (indexed fulltext, else download + convert)."""
    try:
        zot = get_zotero_client()
        item = zot.item(item_key)
        if not item:
            return f"No item found with key: {item_key}"

        metadata = format_item_metadata(item, include_abstract=True)

        attachment = get_attachment_details(zot, item)
        if not attachment:
            return f"{metadata}\n\n---\n\nNo suitable attachment found for this item."

        # Try Zotero's own full-text index first.
        try:
            full_text_data = zot.fulltext_item(attachment.key)
            if full_text_data and full_text_data.get("content"):
                return f"{metadata}\n\n---\n\n## Full Text\n\n{full_text_data['content']}"
        except Exception as fulltext_error:
            logger.debug("No indexed full text: %s", fulltext_error)

        # Fallback: download the attachment and convert to markdown.
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                file_path = os.path.join(tmpdir, attachment.filename or f"{attachment.key}.pdf")
                zot.dump(attachment.key, filename=os.path.basename(file_path), path=tmpdir)
                if os.path.exists(file_path):
                    converted_text = convert_to_markdown(file_path)
                    return f"{metadata}\n\n---\n\n## Full Text\n\n{converted_text}"
                return f"{metadata}\n\n---\n\nFile download failed."
        except Exception as download_error:
            logger.error("Error downloading/converting file: %s", download_error)
            return f"{metadata}\n\n---\n\nError accessing attachment: {str(download_error)}"

    except Exception as e:
        logger.error("Error fetching item full text: %s", e)
        return f"Error fetching item full text: {str(e)}"


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

def get_collections(limit: int | str | None = None) -> str:
    """List all collections, rendered as a hierarchy where possible."""
    try:
        zot = get_zotero_client()
        limit = _coerce_limit(limit)
        collections = zot.collections(limit=limit)

        output = ["# Zotero Collections", ""]
        if not collections:
            output.append("No collections found in your Zotero library.")
            return "\n".join(output)

        collection_map = {c["key"]: c for c in collections}
        hierarchy: dict[str | None, list[str]] = {}
        for coll in collections:
            parent_key = coll["data"].get("parentCollection")
            if parent_key in ["", None] or not parent_key:
                parent_key = None
            hierarchy.setdefault(parent_key, []).append(coll["key"])

        def format_collection(key: str, level: int = 0) -> list[str]:
            if key not in collection_map:
                return []
            coll = collection_map[key]
            name = coll["data"].get("name", "Unnamed Collection")
            indent = "  " * level
            lines = [f"{indent}- **{name}** (Key: {key})"]
            for child_key in sorted(hierarchy.get(key, [])):
                lines.extend(format_collection(child_key, level + 1))
            return lines

        top_level_keys = hierarchy.get(None, [])
        if not top_level_keys:
            output.append("Collections (flat list):")
            for coll in sorted(collections, key=lambda x: x["data"].get("name", "")):
                name = coll["data"].get("name", "Unnamed Collection")
                output.append(f"- **{name}** (Key: {coll['key']})")
        else:
            for key in sorted(top_level_keys):
                output.extend(format_collection(key))

        return "\n".join(output)

    except Exception as e:
        logger.error("Error fetching collections: %s", e)
        return f"# Zotero Collections\n\nError fetching collections: {str(e)}"


def get_collection_items(collection_key: str, limit: int | str | None = 50) -> str:
    """List items in a specific collection."""
    try:
        zot = get_zotero_client()
        try:
            collection = zot.collection(collection_key)
            collection_name = collection["data"].get("name", "Unnamed Collection")
        except Exception:
            collection_name = f"Collection {collection_key}"

        limit = _coerce_limit(limit)
        items = zot.collection_items(collection_key, limit=limit)
        if not items:
            return f"No items found in collection: {collection_name} (Key: {collection_key})"

        output = [f"# Items in Collection: {collection_name}", ""]
        for i, item in enumerate(items, 1):
            data = item.get("data", {})
            title = data.get("title", "Untitled")
            item_type = data.get("itemType", "unknown")
            date = data.get("date", "No date")
            key = item.get("key", "")
            creators_str = format_creators(data.get("creators", []))
            output.append(f"## {i}. {title}")
            output.append(f"**Type:** {item_type}")
            output.append(f"**Item Key:** {key}")
            output.append(f"**Date:** {date}")
            output.append(f"**Authors:** {creators_str}")
            output.append("")

        return "\n".join(output)

    except Exception as e:
        logger.error("Error fetching collection items: %s", e)
        return f"Error fetching collection items: {str(e)}"


def get_item_children(item_key: str) -> str:
    """List child items (attachments, notes, others) for an item."""
    try:
        zot = get_zotero_client()
        try:
            parent = zot.item(item_key)
            parent_title = parent["data"].get("title", "Untitled Item")
        except Exception:
            parent_title = f"Item {item_key}"

        children = zot.children(item_key)
        if not children:
            return f"No child items found for: {parent_title} (Key: {item_key})"

        output = [f"# Child Items for: {parent_title}", ""]
        attachments, notes, others = [], [], []
        for child in children:
            item_type = child.get("data", {}).get("itemType", "unknown")
            if item_type == "attachment":
                attachments.append(child)
            elif item_type == "note":
                notes.append(child)
            else:
                others.append(child)

        if attachments:
            output.append("## Attachments")
            for i, att in enumerate(attachments, 1):
                data = att.get("data", {})
                output.append(f"{i}. **{data.get('title', 'Untitled')}**")
                output.append(f"   - Key: {att.get('key', '')}")
                output.append(f"   - Type: {data.get('contentType', 'Unknown')}")
                if filename := data.get("filename", ""):
                    output.append(f"   - Filename: {filename}")
                output.append("")

        if notes:
            output.append("## Notes")
            for i, note in enumerate(notes, 1):
                data = note.get("data", {})
                note_text = data.get("note", "")
                note_text = note_text.replace("<p>", "").replace("</p>", "\n\n")
                note_text = note_text.replace("<br/>", "\n").replace("<br>", "\n")
                if len(note_text) > 500:
                    note_text = note_text[:500] + "...\n\n(Note truncated)"
                output.append(f"{i}. **{data.get('title', 'Untitled Note')}**")
                output.append(f"   - Key: {note.get('key', '')}")
                output.append(f"   - Content:\n```\n{note_text}\n```")
                output.append("")

        if others:
            output.append("## Other Items")
            for i, other in enumerate(others, 1):
                data = other.get("data", {})
                output.append(f"{i}. **{data.get('title', 'Untitled')}**")
                output.append(f"   - Key: {other.get('key', '')}")
                output.append(f"   - Type: {data.get('itemType', 'unknown')}")
                output.append("")

        return "\n".join(output)

    except Exception as e:
        logger.error("Error fetching item children: %s", e)
        return f"Error fetching item children: {str(e)}"


# ---------------------------------------------------------------------------
# Tags / libraries
# ---------------------------------------------------------------------------

def get_tags(limit: int | str | None = None) -> str:
    """List all tags, grouped alphabetically."""
    try:
        zot = get_zotero_client()
        limit = _coerce_limit(limit)
        tags = zot.tags(limit=limit)
        if not tags:
            return "No tags found in your Zotero library."

        output = ["# Zotero Tags", ""]
        current_letter = None
        for tag in sorted(tags):
            first_letter = tag[0].upper() if tag else "#"
            if first_letter != current_letter:
                current_letter = first_letter
                output.append(f"## {current_letter}")
            output.append(f"- `{tag}`")

        return "\n".join(output)

    except Exception as e:
        logger.error("Error fetching tags: %s", e)
        return f"Error fetching tags: {str(e)}"


def list_libraries() -> str:
    """List accessible libraries (user, group, RSS feeds)."""
    try:
        local = os.getenv("ZOTERO_LOCAL", "").lower() in ["true", "yes", "1"]
        override = get_active_library()

        output = ["# Zotero Libraries", ""]
        if override:
            output.append(
                f"> **Active library:** ID={override['library_id']}, "
                f"type={override['library_type']}"
            )
            output.append("")

        if local:
            from local_db import LocalZoteroReader

            reader = LocalZoteroReader(db_path=_local_db_path())
            try:
                libraries = reader.get_libraries()

                user_libs = [l for l in libraries if l["type"] == "user"]
                if user_libs:
                    output.append("## User Library")
                    for lib in user_libs:
                        output.append(
                            f"- **My Library** — {lib['itemCount']} items "
                            f"(libraryID={lib['libraryID']})"
                        )
                    output.append("")

                group_libs = [l for l in libraries if l["type"] == "group"]
                if group_libs:
                    output.append("## Group Libraries")
                    for lib in group_libs:
                        desc = f" — {lib['groupDescription']}" if lib.get("groupDescription") else ""
                        output.append(
                            f"- **{lib['groupName']}** — {lib['itemCount']} items "
                            f"(groupID={lib['groupID']}){desc}"
                        )
                    output.append("")

                feed_libs = [l for l in libraries if l["type"] == "feed"]
                if feed_libs:
                    output.append("## RSS Feeds")
                    for lib in feed_libs:
                        output.append(
                            f"- **{lib['feedName']}** — {lib['itemCount']} items "
                            f"(libraryID={lib['libraryID']})"
                        )
                    output.append("")
            finally:
                reader.close()
        else:
            zot = get_zotero_client()
            output.append("## User Library")
            output.append(
                f"- **My Library** (libraryID={os.getenv('ZOTERO_LIBRARY_ID', '?')})"
            )
            output.append("")
            try:
                groups = zot.groups()
                if groups:
                    output.append("## Group Libraries")
                    for group in groups:
                        gdata = group.get("data", {})
                        output.append(
                            f"- **{gdata.get('name', 'Unknown')}** "
                            f"(groupID={group.get('id', '?')})"
                        )
                    output.append("")
            except Exception:
                output.append("*Could not retrieve group libraries.*\n")
            output.append("*Note: RSS feeds are only accessible in local mode.*")

        return "\n".join(output)

    except Exception as e:
        logger.error("Error listing libraries: %s", e)
        return f"Error listing libraries: {str(e)}"


# ---------------------------------------------------------------------------
# Advanced search
# ---------------------------------------------------------------------------

def advanced_search(
    conditions: list[dict[str, str]] | str,
    join_mode: str = "all",
    sort_by: str | None = None,
    sort_direction: str = "asc",
    limit: int | str = 50,
) -> str:
    """Advanced multi-condition search with client-side filtering and sorting."""
    try:
        if isinstance(conditions, str):
            try:
                conditions = json.loads(conditions)
            except json.JSONDecodeError as parse_error:
                return (
                    "Error: conditions must be valid JSON when provided as a string "
                    f"({parse_error})"
                )

        if not isinstance(conditions, list) or not conditions:
            return "Error: No search conditions provided"
        if join_mode not in {"all", "any"}:
            return "Error: join_mode must be either 'all' or 'any'"

        limit_n = int(limit) if isinstance(limit, str) else int(limit or 0)
        if limit_n <= 0:
            return "Error: limit must be greater than 0"
        if limit_n > 500:
            limit_n = 500

        zot = get_zotero_client()

        valid_operations = {
            "is", "isNot", "contains", "doesNotContain", "beginsWith", "endsWith",
            "isGreaterThan", "isLessThan", "isBefore", "isAfter",
        }

        parsed_conditions: list[dict[str, str]] = []
        for i, condition in enumerate(conditions, 1):
            if not isinstance(condition, dict):
                return f"Error: Condition {i} must be an object"
            if "field" not in condition or "operation" not in condition or "value" not in condition:
                return f"Error: Condition {i} is missing required fields (field, operation, value)"
            field = str(condition["field"]).strip()
            operation = str(condition["operation"]).strip()
            value = str(condition["value"]).strip()
            if operation not in valid_operations:
                return (
                    f"Error: Unsupported operation '{operation}' in condition {i}. "
                    f"Supported: {', '.join(sorted(valid_operations))}"
                )
            if not field:
                return f"Error: Condition {i} has an empty field"
            parsed_conditions.append({"field": field, "operation": operation, "value": value})

        def _extract_values(data: dict, field: str) -> list[str]:
            field_lower = field.lower()
            if field_lower in {"author", "authors", "creator", "creators"}:
                creators = data.get("creators", []) or []
                values: list[str] = []
                for creator in creators:
                    if not isinstance(creator, dict):
                        continue
                    if creator.get("firstName") or creator.get("lastName"):
                        full_name = " ".join(
                            [str(creator.get("firstName", "")).strip(),
                             str(creator.get("lastName", "")).strip()]
                        ).strip()
                        if full_name:
                            values.append(full_name)
                    if creator.get("name"):
                        values.append(str(creator.get("name", "")).strip())
                return values
            if field_lower in {"tag", "tags"}:
                values = []
                for tag in data.get("tags", []) or []:
                    if isinstance(tag, dict) and tag.get("tag"):
                        values.append(str(tag.get("tag", "")).strip())
                return values
            if field_lower == "year":
                date_value = str(data.get("date", "")).strip()
                return [date_value[:4]] if len(date_value) >= 4 else []
            field_aliases = {
                "itemtype": "itemType", "dateadded": "dateAdded",
                "datemodified": "dateModified", "doi": "DOI",
            }
            source_field = field_aliases.get(field_lower, field)
            raw_value = data.get(source_field, "")
            if raw_value is None:
                return []
            return [str(raw_value).strip()]

        def _as_float(text: str) -> float | None:
            try:
                return float(text)
            except ValueError:
                return None

        def _compare(candidate: str, expected: str, operation: str) -> bool:
            left, right = candidate.lower(), expected.lower()
            if operation == "is":
                return left == right
            if operation == "isNot":
                return left != right
            if operation == "contains":
                return right in left
            if operation == "doesNotContain":
                return right not in left
            if operation == "beginsWith":
                return left.startswith(right)
            if operation == "endsWith":
                return left.endswith(right)
            left_num, right_num = _as_float(left), _as_float(right)
            if (operation in {"isGreaterThan", "isLessThan", "isBefore", "isAfter"}
                    and left_num is not None and right_num is not None):
                if operation in {"isGreaterThan", "isAfter"}:
                    return left_num > right_num
                return left_num < right_num
            if operation in {"isGreaterThan", "isAfter"}:
                return left > right
            return left < right

        def _matches_condition(data: dict, condition: dict) -> bool:
            values = _extract_values(data, condition["field"])
            if not values:
                return False
            operation = condition["operation"]
            target = condition["value"]
            comparisons = [_compare(value, target, operation) for value in values]
            if operation in {"isNot", "doesNotContain"}:
                return all(comparisons)
            return any(comparisons)

        results = []
        batch_size = 100
        start = 0
        while True:
            batch = zot.items(start=start, limit=batch_size)
            if not batch:
                break
            for item in batch:
                data = item.get("data", {})
                if data.get("itemType") in {"attachment", "note", "annotation"}:
                    continue
                checks = [_matches_condition(data, c) for c in parsed_conditions]
                matched = all(checks) if join_mode == "all" else any(checks)
                if matched:
                    results.append(item)
            if len(batch) < batch_size:
                break
            start += batch_size

        if sort_by:
            sort_field = sort_by.strip()
            reverse = sort_direction == "desc"

            def _sort_key(item: dict) -> str:
                data = item.get("data", {}) if isinstance(item, dict) else {}
                if sort_field in {"creator", "author"}:
                    return format_creators(data.get("creators", []))
                return str(data.get(sort_field, "")).lower()

            results.sort(key=_sort_key, reverse=reverse)

        if not results:
            return "No items found matching the search criteria."

        results = results[:limit_n]

        output = ["# Advanced Search Results", ""]
        output.append(f"Found {len(results)} items matching the search criteria:")
        output.append("")
        output.append("## Search Criteria")
        output.append(f"Join mode: {join_mode.upper()}")
        for i, condition in enumerate(parsed_conditions, 1):
            output.append(f"{i}. {condition['field']} {condition['operation']} \"{condition['value']}\"")
        output.append("")
        output.append("## Results")
        for i, item in enumerate(results, 1):
            _format_item_entry(output, i, item, heading="###")

        return "\n".join(output)

    except Exception as e:
        logger.error("Error in advanced search: %s", e)
        return f"Error in advanced search: {str(e)}"


# ---------------------------------------------------------------------------
# Annotations & notes
# ---------------------------------------------------------------------------

def get_annotations(
    item_key: str | None = None,
    use_pdf_extraction: bool = False,
    limit: int | str | None = None,
) -> str:
    """Get annotations for an item (Better BibTeX → Zotero API → PDF fallback) or library-wide."""
    try:
        zot = get_zotero_client()
        annotations: list[dict] = []
        parent_title = "Untitled Item"

        if item_key:
            try:
                parent = zot.item(item_key)
                parent_title = parent["data"].get("title", "Untitled Item")
            except Exception:
                return f"Error: No item found with key: {item_key}"

            better_bibtex_annotations: list[dict] = []
            zotero_api_annotations: list[dict] = []
            pdf_annotations: list[dict] = []

            if os.environ.get("ZOTERO_LOCAL", "").lower() in ["true", "yes", "1"]:
                try:
                    from better_bibtex_client import (
                        ZoteroBetterBibTexAPI,
                        get_color_category,
                        process_annotation,
                    )

                    bibtex = ZoteroBetterBibTexAPI()
                    if bibtex.is_zotero_running():
                        citation_key = None
                        try:
                            extra_field = parent["data"].get("extra", "")
                            for line in extra_field.split("\n"):
                                if line.lower().startswith("citation key:"):
                                    citation_key = line.replace("Citation Key:", "").strip()
                                    break
                                elif line.lower().startswith("citationkey:"):
                                    citation_key = line.replace("citationkey:", "").strip()
                                    break
                        except Exception as e:
                            logger.warning("Error extracting citation key: %s", e)

                        if not citation_key:
                            title = parent["data"].get("title", "")
                            try:
                                if title:
                                    for result in bibtex.search_citekeys(title):
                                        if result.get("citekey"):
                                            citation_key = result["citekey"]
                                            break
                            except Exception as e:
                                logger.warning("Error searching for citation key: %s", e)

                        if citation_key:
                            try:
                                library = "*"
                                search_results = bibtex._make_request("item.search", [citation_key])
                                if search_results:
                                    matched_item = next(
                                        (it for it in search_results if it.get("citekey") == citation_key),
                                        None,
                                    )
                                    if matched_item:
                                        library = matched_item.get("library", "*")

                                attachments = bibtex.get_attachments(citation_key, library)
                                for attachment in attachments:
                                    for anno in bibtex.get_annotations_from_attachment(attachment):
                                        processed = process_annotation(anno, attachment)
                                        if processed:
                                            better_bibtex_annotations.append({
                                                "key": processed.get("id", ""),
                                                "data": {
                                                    "itemType": "annotation",
                                                    "annotationType": processed.get("type", "highlight"),
                                                    "annotationText": processed.get("annotatedText", ""),
                                                    "annotationComment": processed.get("comment", ""),
                                                    "annotationColor": processed.get("color", ""),
                                                    "parentItem": item_key,
                                                    "tags": [],
                                                    "_pdf_page": processed.get("page", 0),
                                                    "_pageLabel": processed.get("pageLabel", ""),
                                                    "_attachment_title": attachment.get("title", ""),
                                                    "_color_category": get_color_category(processed.get("color", "")),
                                                    "_from_better_bibtex": True,
                                                },
                                            })
                            except Exception as e:
                                logger.warning("Error processing Better BibTeX annotations: %s", e)
                except Exception as bibtex_error:
                    logger.warning("Error initializing Better BibTeX: %s", bibtex_error)

            if not better_bibtex_annotations:
                try:
                    children = zot.children(item_key)
                    zotero_api_annotations = [
                        item for item in children
                        if item.get("data", {}).get("itemType") == "annotation"
                    ]
                except Exception as api_error:
                    logger.warning("Error retrieving Zotero API annotations: %s", api_error)

            if use_pdf_extraction and not (better_bibtex_annotations or zotero_api_annotations):
                try:
                    from pdfannots_helper import (
                        ensure_pdfannots_installed,
                        extract_annotations_from_pdf,
                    )

                    if ensure_pdfannots_installed():
                        children = zot.children(item_key)
                        pdf_attachments = [
                            item for item in children
                            if item.get("data", {}).get("contentType") == "application/pdf"
                        ]
                        for attachment in pdf_attachments:
                            with tempfile.TemporaryDirectory() as tmpdir:
                                att_key = attachment.get("key", "")
                                file_path = os.path.join(tmpdir, f"{att_key}.pdf")
                                zot.dump(att_key, filename=os.path.basename(file_path), path=tmpdir)
                                if os.path.exists(file_path):
                                    for ext in extract_annotations_from_pdf(file_path, tmpdir):
                                        if not ext.get("annotatedText") and not ext.get("comment"):
                                            continue
                                        pdf_anno = {
                                            "key": f"pdf_{att_key}_{ext.get('id', uuid.uuid4().hex[:8])}",
                                            "data": {
                                                "itemType": "annotation",
                                                "annotationType": ext.get("type", "highlight"),
                                                "annotationText": ext.get("annotatedText", ""),
                                                "annotationComment": ext.get("comment", ""),
                                                "annotationColor": ext.get("color", ""),
                                                "parentItem": item_key,
                                                "tags": [],
                                                "_pdf_page": ext.get("page", 0),
                                                "_from_pdf_extraction": True,
                                                "_attachment_title": attachment.get("data", {}).get("title", "PDF"),
                                            },
                                        }
                                        if ext.get("type") == "image" and ext.get("imageRelativePath"):
                                            pdf_anno["data"]["_image_path"] = os.path.join(
                                                tmpdir, ext.get("imageRelativePath")
                                            )
                                        pdf_annotations.append(pdf_anno)
                except Exception as pdf_error:
                    logger.warning("Error during PDF annotation extraction: %s", pdf_error)

            annotations = better_bibtex_annotations + zotero_api_annotations + pdf_annotations
        else:
            limit = _coerce_limit(limit)
            zot.add_parameters(itemType="annotation", limit=limit or 50)
            annotations = zot.everything(zot.items())

        if not annotations:
            return f"No annotations found{f' for item: {parent_title}' if item_key else ''}."

        output = [f"# Annotations{f' for: {parent_title}' if item_key else ''}", ""]
        for i, anno in enumerate(annotations, 1):
            data = anno.get("data", {})
            anno_type = data.get("annotationType", "Unknown type")
            anno_text = data.get("annotationText", "")
            anno_comment = data.get("annotationComment", "")
            anno_color = data.get("annotationColor", "")
            anno_key = anno.get("key", "")

            parent_info = ""
            if not item_key and (parent_key := data.get("parentItem")):
                try:
                    parent = zot.item(parent_key)
                    parent_title = parent["data"].get("title", "Untitled")
                    parent_info = f' (from "{parent_title}")'
                except Exception:
                    parent_info = f" (parent key: {parent_key})"

            source_info = ""
            if data.get("_from_better_bibtex", False):
                source_info = " (extracted via Better BibTeX)"
            elif data.get("_from_pdf_extraction", False):
                source_info = " (extracted directly from PDF)"

            attachment_info = ""
            if data.get("_attachment_title"):
                attachment_info = f" in {data['_attachment_title']}"

            output.append(f"## Annotation {i}{parent_info}{attachment_info}{source_info}")
            output.append(f"**Type:** {anno_type}")
            output.append(f"**Key:** {anno_key}")
            if anno_color:
                output.append(f"**Color:** {anno_color}")
                if data.get("_color_category"):
                    output.append(f"**Color Category:** {data['_color_category']}")
            if "_pdf_page" in data:
                label = data.get("_pageLabel", str(data["_pdf_page"]))
                output.append(f"**Page:** {data['_pdf_page']} (Label: {label})")
            if anno_text:
                output.append(f"**Text:** {anno_text}")
            if anno_comment:
                output.append(f"**Comment:** {anno_comment}")
            if "_image_path" in data and os.path.exists(data["_image_path"]):
                output.append("**Image:** This annotation includes an image (not displayed).")
            if tags := data.get("tags"):
                tag_list = [f"`{tag['tag']}`" for tag in tags]
                if tag_list:
                    output.append(f"**Tags:** {' '.join(tag_list)}")
            output.append("")

        return "\n".join(output)

    except Exception as e:
        logger.error("Error fetching annotations: %s", e)
        return f"Error fetching annotations: {str(e)}"


def get_notes(
    item_key: str | None = None,
    limit: int | str | None = 20,
    truncate: bool = True,
) -> str:
    """Retrieve notes (optionally for a single parent item)."""
    try:
        zot = get_zotero_client()
        params = {"itemType": "note"}
        limit = _coerce_limit(limit)

        if item_key:
            notes = zot.children(item_key, **params) if not limit else zot.children(item_key, limit=limit, **params)
        else:
            notes = zot.items(**params) if not limit else zot.items(limit=limit, **params)

        if not notes:
            return f"No notes found{f' for item {item_key}' if item_key else ''}."

        output = [f"# Notes{f' for Item: {item_key}' if item_key else ''}", ""]
        for i, note in enumerate(notes, 1):
            data = note.get("data", {})
            note_key = note.get("key", "")
            parent_info = ""
            if parent_key := data.get("parentItem"):
                try:
                    parent = zot.item(parent_key)
                    parent_title = parent["data"].get("title", "Untitled")
                    parent_info = f' (from "{parent_title}")'
                except Exception:
                    parent_info = f" (parent key: {parent_key})"

            note_text = clean_html(data.get("note", ""))
            if truncate and len(note_text) > 500:
                note_text = note_text[:500] + "..."

            output.append(f"## Note {i}{parent_info}")
            output.append(f"**Key:** {note_key}")
            if tags := data.get("tags"):
                tag_list = [f"`{tag['tag']}`" for tag in tags]
                if tag_list:
                    output.append(f"**Tags:** {' '.join(tag_list)}")
            output.append(f"**Content:**\n{note_text}")
            output.append("")

        return "\n".join(output)

    except Exception as e:
        logger.error("Error fetching notes: %s", e)
        return f"Error fetching notes: {str(e)}"


def search_notes(query: str, limit: int | str | None = 20) -> str:
    """Search notes and annotations by keyword, with context highlighting."""
    try:
        if not query.strip():
            return "Error: Search query cannot be empty"

        zot = get_zotero_client()
        limit = _coerce_limit(limit)

        zot.add_parameters(q=query, qmode="everything", itemType="note", limit=limit or 20)
        notes = zot.items()

        annotation_results = get_annotations(item_key=None, use_pdf_extraction=True, limit=limit or 20)

        # Parse annotation markdown blocks back out of the annotation output.
        annotations: list[dict] = []
        current_annotation: dict | None = None
        for line in annotation_results.split("\n"):
            if line.startswith("## "):
                if current_annotation:
                    annotations.append(current_annotation)
                current_annotation = {"lines": [line], "type": "annotation"}
            elif current_annotation is not None:
                current_annotation["lines"].append(line)
        if current_annotation:
            annotations.append(current_annotation)

        query_lower = query.lower()
        query_terms = query_lower.split()
        note_results = []
        for note in notes:
            data = note.get("data", {})
            note_text = data.get("note", "").lower()
            if all(term in note_text for term in query_terms):
                note_results.append({"type": "note", "key": note.get("key", ""), "data": data})

        annotation_results_filtered = []
        for annotation in annotations:
            block_text = "\n".join(annotation.get("lines", []))
            if query_lower in block_text.lower():
                annotation_results_filtered.append(annotation)

        all_results = note_results + annotation_results_filtered
        if not all_results:
            return f"No results found for '{query}'"

        output = [f"# Search Results for '{query}'", ""]
        for i, result in enumerate(all_results, 1):
            if result["type"] == "note":
                data = result["data"]
                key = result["key"]
                parent_info = ""
                if parent_key := data.get("parentItem"):
                    try:
                        parent = zot.item(parent_key)
                        parent_title = parent["data"].get("title", "Untitled")
                        parent_info = f' (from "{parent_title}")'
                    except Exception:
                        parent_info = f" (parent key: {parent_key})"

                note_text = data.get("note", "")
                note_text = note_text.replace("<p>", "").replace("</p>", "\n\n")
                note_text = note_text.replace("<br/>", "\n").replace("<br>", "\n")
                try:
                    text_lower = note_text.lower()
                    pos = text_lower.find(query_lower)
                    if pos >= 0:
                        start = max(0, pos - 100)
                        end = min(len(note_text), pos + 200)
                        context = note_text[start:end]
                        match = context[context.lower().find(query_lower):context.lower().find(query_lower) + len(query)]
                        note_text = context.replace(match, f"**{match}**") + "..."
                except Exception:
                    note_text = note_text[:500] + "..."

                output.append(f"## Note {i}{parent_info}")
                output.append(f"**Key:** {key}")
                if tags := data.get("tags"):
                    tag_list = [f"`{tag['tag']}`" for tag in tags]
                    if tag_list:
                        output.append(f"**Tags:** {' '.join(tag_list)}")
                output.append(f"**Content:**\n{note_text}")
                output.append("")
            elif result["type"] == "annotation":
                output.extend(result["lines"])
                output.append("")

        return "\n".join(output)

    except Exception as e:
        logger.error("Error searching notes: %s", e)
        return f"Error searching notes: {str(e)}"
