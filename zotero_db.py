import json
import sqlite3


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
