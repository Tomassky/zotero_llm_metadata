import json
import sqlite3
import logging

logger = logging.getLogger(__name__)


def cleanup_llm_tags(db_path: str, prefix: str = "__llm_import__") -> dict:
    stats = {"tags_found": 0, "item_tags_deleted": 0, "tags_deleted": 0}
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        cur = conn.cursor()
        cur.execute("SELECT tagID FROM tags WHERE name LIKE ?", (prefix + "%",))
        tag_ids = [row[0] for row in cur.fetchall()]
        stats["tags_found"] = len(tag_ids)
        if not tag_ids:
            return stats
        placeholders = ",".join("?" * len(tag_ids))
        cur.execute(f"DELETE FROM itemTags WHERE tagID IN ({placeholders})", tag_ids)
        stats["item_tags_deleted"] = cur.rowcount
        cur.execute(f"DELETE FROM tags WHERE tagID IN ({placeholders})", tag_ids)
        stats["tags_deleted"] = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return stats


def reparent_attachments_in_db(db_path: str, mappings: list[dict], cleanup_tag: bool = False) -> dict:
    stats: dict = {"moved": 0, "failed": 0, "errors": []}
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        for m in mappings:
            att_key = m.get("attachment_key", "")
            parent_key = m.get("parent_key", "")
            tag = m.get("tag", "")
            if not att_key or not parent_key:
                stats["failed"] += 1
                stats["errors"].append(f"Missing keys for mapping: {m}")
                continue
            row = cur.execute("SELECT itemID FROM items WHERE key=?", (att_key,)).fetchone()
            prow = cur.execute("SELECT itemID FROM items WHERE key=?", (parent_key,)).fetchone()
            if not row or not prow:
                stats["failed"] += 1
                stats["errors"].append(f"ItemID not found for {att_key} or {parent_key}")
                continue
            att_id, parent_id = row[0], prow[0]
            cur.execute("UPDATE itemAttachments SET parentItemID=? WHERE itemID=?", (parent_id, att_id))
            if cleanup_tag and tag:
                tag_row = cur.execute("SELECT tagID FROM tags WHERE name=?", (tag,)).fetchone()
                if tag_row:
                    cur.execute("DELETE FROM itemTags WHERE itemID=? AND tagID=?", (parent_id, tag_row[0]))
            stats["moved"] += 1
        conn.commit()
    finally:
        conn.close()
    return stats


def update_abstract_in_db(db_path: str, item_key: str, abstract_note: str) -> tuple[bool, str]:
    """Directly write abstractNote into Zotero's SQLite database for a given item key."""
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        cur = conn.cursor()

        row = cur.execute("SELECT itemID FROM items WHERE key=?", (item_key,)).fetchone()
        if not row:
            return False, f"item key {item_key} not found in database"
        item_id = row[0]

        field_row = cur.execute("SELECT fieldID FROM fields WHERE fieldName=?", ("abstractNote",)).fetchone()
        if not field_row:
            return False, "field 'abstractNote' not found in database schema"
        field_id = field_row[0]

        val_row = cur.execute("SELECT valueID FROM itemDataValues WHERE value=?", (abstract_note,)).fetchone()
        if val_row:
            value_id = val_row[0]
        else:
            cur.execute("INSERT INTO itemDataValues (value) VALUES (?)", (abstract_note,))
            value_id = cur.lastrowid

        cur.execute(
            "INSERT OR REPLACE INTO itemData (itemID, fieldID, valueID) VALUES (?, ?, ?)",
            (item_id, field_id, value_id),
        )
        conn.commit()
        return True, "updated abstractNote in database"
    except Exception as e:
        return False, str(e)
    finally:
        conn.close()


def load_abstract_mappings_from_jsonl(jsonl_path: str) -> tuple[list[dict], dict]:
    """Load (key, abstractNote) pairs from fill_abstracts.jsonl for offline apply."""
    mappings = []
    info = {
        "total_lines": 0,
        "parsed_lines": 0,
        "valid_mappings": 0,
        "invalid_lines": 0,
        "missing_keys": 0,
    }
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for raw in f:
            info["total_lines"] += 1
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                info["parsed_lines"] += 1
            except Exception:
                info["invalid_lines"] += 1
                continue
            key = str(rec.get("key", "")).strip()
            abstract = str(rec.get("abstractNote", "")).strip()
            if not key or not abstract:
                info["missing_keys"] += 1
                continue
            mappings.append({"key": key, "abstractNote": abstract})
            info["valid_mappings"] += 1
    return mappings, info


def apply_abstracts_from_mappings(db_path: str, mappings: list[dict]) -> dict:
    """Write abstractNote into DB for each mapping. Returns stats."""
    stats: dict = {"written": 0, "failed": 0, "errors": []}
    for m in mappings:
        ok, msg = update_abstract_in_db(db_path, m["key"], m["abstractNote"])
        if ok:
            stats["written"] += 1
        else:
            stats["failed"] += 1
            stats["errors"].append(f"{m['key']}: {msg}")
    return stats


def load_repair_mappings_from_jsonl(jsonl_path: str) -> tuple[list[dict], dict]:
    mappings = []
    info = {
        "total_lines": 0,
        "parsed_lines": 0,
        "valid_mappings": 0,
        "invalid_lines": 0,
        "missing_keys": 0,
    }
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for raw in f:
            info["total_lines"] += 1
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                info["parsed_lines"] += 1
            except Exception:
                info["invalid_lines"] += 1
                continue
            attachment_key = str(rec.get("item_key", "")).strip()
            write_status = rec.get("write_status")
            parent_key = ""
            if isinstance(write_status, dict):
                parent_key = str(write_status.get("parent_key", "")).strip()
            if not attachment_key or not parent_key:
                info["missing_keys"] += 1
                continue
            mappings.append({
                "attachment_key": attachment_key,
                "parent_key": parent_key,
                "tag": f"__llm_import__{attachment_key}",
            })
            info["valid_mappings"] += 1
    return mappings, info


# ---------------------------------------------------------------------------
# Tag writing (for --fill-tags)
# ---------------------------------------------------------------------------

def write_tags_to_db(db_path: str, item_key: str, tags: list[str]) -> dict:
    """将 tags 写入 Zotero SQLite 的 itemTags 表。

    Args:
        db_path: Zotero SQLite 数据库路径
        item_key: Zotero item key (short hash)
        tags: list of tag name strings

    Returns:
        {"written": int, "skipped": int, "errors": list}
    """
    stats: dict = {"written": 0, "skipped": 0, "errors": []}
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        cur = conn.cursor()

        # Find itemID
        row = cur.execute("SELECT itemID FROM items WHERE key=?", (item_key,)).fetchone()
        if not row:
            stats["errors"].append(f"item key '{item_key}' not found")
            return stats
        item_id = row[0]

        for tag_name in tags:
            tag_name = tag_name.strip()
            if not tag_name:
                stats["skipped"] += 1
                continue

            # Find or create tagID
            tag_row = cur.execute("SELECT tagID FROM tags WHERE name=?", (tag_name,)).fetchone()
            if tag_row:
                tag_id = tag_row[0]
            else:
                cur.execute("INSERT INTO tags (name) VALUES (?)", (tag_name,))
                tag_id = cur.lastrowid

            # Insert itemTags (ignore if already exists)
            try:
                cur.execute(
                    "INSERT OR IGNORE INTO itemTags (itemID, tagID, type) VALUES (?, ?, 0)",
                    (item_id, tag_id),
                )
                if cur.rowcount > 0:
                    stats["written"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as e:
                stats["errors"].append(f"tag '{tag_name}': {e}")

        conn.commit()
    except Exception as e:
        stats["errors"].append(str(e))
    finally:
        conn.close()
    return stats


def load_tags_mappings_from_jsonl(jsonl_path: str) -> tuple[list[dict], dict]:
    """从 fill_tags.jsonl 加载 (key, tags) 映射。

    Returns:
        (mappings, info) where each mapping has {"key": "...", "tags": [...]}
    """
    mappings = []
    info = {
        "total_lines": 0,
        "parsed_lines": 0,
        "valid_mappings": 0,
        "invalid_lines": 0,
        "missing_keys": 0,
    }
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for raw in f:
            info["total_lines"] += 1
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                info["parsed_lines"] += 1
            except Exception:
                info["invalid_lines"] += 1
                continue
            key = str(rec.get("key", "")).strip()
            tags = rec.get("tags", [])
            if not key or not isinstance(tags, list) or not tags:
                info["missing_keys"] += 1
                continue
            # Filter empty tags
            tags = [t.strip() for t in tags if isinstance(t, str) and t.strip()]
            if not tags:
                info["missing_keys"] += 1
                continue
            mappings.append({"key": key, "tags": tags})
            info["valid_mappings"] += 1
    return mappings, info


def apply_tags_from_mappings(db_path: str, mappings: list[dict]) -> dict:
    """批量将 tags 写入 Zotero SQLite。Returns stats."""
    total_stats: dict = {"written": 0, "skipped": 0, "failed": 0, "errors": []}
    for m in mappings:
        stats = write_tags_to_db(db_path, m["key"], m["tags"])
        total_stats["written"] += stats["written"]
        total_stats["skipped"] += stats["skipped"]
        if stats["errors"]:
            total_stats["failed"] += 1
            total_stats["errors"].extend(stats["errors"])
    return total_stats
