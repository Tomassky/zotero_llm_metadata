import sys
from typing import Optional
from urllib.parse import quote, urlencode

try:
    import httpx
except Exception:
    print("Missing dependency: httpx. Install with `python3 -m pip install httpx`.", file=sys.stderr)
    raise


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def join_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def _user_items_url(base: str, library_id: str, path: str, **params) -> str:
    url = join_url(base, f"users/{library_id}/{path.lstrip('/')}")
    if params:
        url += "?" + urlencode(params)
    return url


# ---------------------------------------------------------------------------
# Item normalization helpers
# ---------------------------------------------------------------------------

def extract_items(payload) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "data", "results"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
    return []


def normalize_item(item) -> tuple[dict, dict]:
    if isinstance(item, dict) and isinstance(item.get("data"), dict):
        return item["data"], item
    return item if isinstance(item, dict) else {}, item


def extract_item_key(data: dict, raw: dict) -> str:
    return (
        data.get("key") or raw.get("key")
        or data.get("itemKey") or raw.get("itemKey")
        or ""
    )


def pick_filename(data: dict) -> Optional[str]:
    for key in ("filename", "fileName", "path", "localPath", "title"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            if key in ("path", "localPath"):
                path = val.replace("\\", "/")
                return path.rsplit("/", 1)[-1]
            return val
    return None


# ---------------------------------------------------------------------------
# Item classification
# ---------------------------------------------------------------------------

def is_no_metadata_attachment(data: dict) -> bool:
    item_type = data.get("itemType") or data.get("type")
    if item_type and item_type != "attachment":
        return False
    if data.get("parentItem") or data.get("parentKey"):
        return False
    return True


def is_missing_abstract(data: dict, include_attachments: bool = False) -> bool:
    item_type = str(data.get("itemType", "")).strip()
    if not include_attachments and item_type in {"attachment", "note", "annotation"}:
        return False
    abstract = data.get("abstractNote")
    if abstract is None:
        return True
    if isinstance(abstract, str):
        return abstract.strip() == ""
    return False


# ---------------------------------------------------------------------------
# Metadata normalization
# ---------------------------------------------------------------------------

def format_creators(creators) -> str:
    if not isinstance(creators, list):
        return ""
    parts: list[str] = []
    for c in creators:
        if not isinstance(c, dict):
            continue
        first = str(c.get("firstName", "")).strip()
        last = str(c.get("lastName", "")).strip()
        name = str(c.get("name", "")).strip()
        if first or last:
            parts.append(" ".join(x for x in (first, last) if x))
        elif name:
            parts.append(name)
    return "; ".join(parts)


def normalize_tags(tags) -> list:
    if not isinstance(tags, list):
        return []
    out = []
    for t in tags:
        if isinstance(t, str) and t.strip():
            out.append({"tag": t.strip()})
        elif isinstance(t, dict) and "tag" in t:
            out.append({"tag": str(t["tag"])})
    return out


def build_item_data(metadata: dict) -> dict:
    allowed_fields = {
        "itemType", "title", "creators", "date", "publicationTitle",
        "publisher", "place", "DOI", "url", "abstractNote", "language", "tags",
    }
    data = {k: metadata[k] for k in allowed_fields if k in metadata}
    if "tags" in data:
        data["tags"] = normalize_tags(data["tags"])
    if "creators" in data and not isinstance(data["creators"], list):
        data["creators"] = []
    return data


# ---------------------------------------------------------------------------
# Paginated item fetching
# ---------------------------------------------------------------------------

def fetch_no_metadata_items(
    client: httpx.Client,
    base: str,
    list_path: str,
    timeout: int,
    limit: int,
    page_size: int = 100,
    max_pages: int = 1000,
) -> list[tuple[str, str]]:
    out = []
    start = 0
    for _ in range(max_pages):
        if len(out) >= limit:
            break
        url = join_url(base, list_path) + "?" + urlencode(
            {"limit": str(page_size), "start": str(start), "itemType": "attachment"}
        )
        resp = client.get(url, timeout=timeout)
        if resp.status_code >= 400:
            print(f"Fetch no-metadata items failed: {resp.status_code} {resp.text}", file=sys.stderr)
            break
        items = extract_items(resp.json())
        if not items:
            break
        for item in items:
            data, raw = normalize_item(item)
            if not is_no_metadata_attachment(data):
                continue
            key = extract_item_key(data, raw)
            if not key:
                continue
            out.append((pick_filename(data) or "(unknown)", str(key)))
            if len(out) >= limit:
                break
        start += page_size
        if len(items) < page_size:
            break
    return out


def fetch_no_abstract_items(
    client: httpx.Client,
    base: str,
    library_id: str,
    endpoint: str,
    timeout: int,
    limit: int,
    page_size: int,
    max_pages: int,
    include_attachments: bool,
) -> list[dict]:
    out: list[dict] = []
    start = 0
    for _ in range(max_pages):
        if len(out) >= limit:
            break
        params: dict = {"limit": str(page_size), "start": str(start)}
        if not include_attachments:
            params["itemType"] = "-attachment"
        url = _user_items_url(base, library_id, endpoint, **params)
        resp = client.get(url, timeout=timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"API request failed: {resp.status_code} {resp.text}")
        items = extract_items(resp.json())
        if not items:
            break
        for item in items:
            data, raw = normalize_item(item)
            if not is_missing_abstract(data, include_attachments):
                continue
            key = extract_item_key(data, raw)
            if not key:
                continue
            out.append({
                "key": str(key),
                "itemType": str(data.get("itemType", "")),
                "title": str(data.get("title", "")).strip(),
                "date": str(data.get("date", "")).strip(),
                "abstractNote": data.get("abstractNote", ""),
            })
            if len(out) >= limit:
                break
        start += page_size
        if len(items) < page_size:
            break
    return out


# ---------------------------------------------------------------------------
# Item read/write
# ---------------------------------------------------------------------------


def get_child_attachment_keys(
    client: httpx.Client,
    base: str,
    library_id: str,
    item_key: str,
    timeout: int,
    limit: int = 15,
) -> list[str]:
    url = join_url(base, f"users/{library_id}/items/{quote(item_key)}/children") + "?" + urlencode(
        {"itemType": "attachment", "limit": str(limit)}
    )
    resp = client.get(url, timeout=timeout)
    if resp.status_code >= 400:
        return []
    out: list[str] = []
    for item in extract_items(resp.json()):
        data, raw = normalize_item(item)
        key = extract_item_key(data, raw)
        if key:
            out.append(str(key))
    return out


def get_local_item_data(
    client: httpx.Client,
    base: str,
    library_id: str,
    item_key: str,
    timeout: int = 90,
) -> dict:
    url = join_url(base, f"users/{library_id}/items/{quote(item_key)}")
    resp = client.get(url, timeout=timeout)
    if resp.status_code >= 400:
        return {}
    data, _ = normalize_item(resp.json())
    return data if isinstance(data, dict) else {}


def find_local_item_by_tag(
    client: httpx.Client,
    base: str,
    library_id: str,
    tag: str,
    timeout: int = 90,
) -> str:
    url = join_url(base, f"users/{library_id}/items?tag={quote(tag)}&limit=1")
    resp = client.get(url, timeout=timeout)
    if resp.status_code >= 400:
        return ""
    items = extract_items(resp.json())
    if not items:
        return ""
    data, raw = normalize_item(items[0])
    return extract_item_key(data, raw)


def get_inherited_collections(
    client: httpx.Client,
    base: str,
    library_id: str,
    attachment_key: str,
    timeout: int = 90,
) -> list[str]:
    data = get_local_item_data(client, base, library_id, attachment_key, timeout=timeout)
    collections = data.get("collections", []) if isinstance(data, dict) else []

    if (not isinstance(collections, list) or not collections) and isinstance(data, dict):
        parent_key = str(data.get("parentItem", "")).strip()
        if parent_key:
            parent_data = get_local_item_data(client, base, library_id, parent_key, timeout=timeout)
            collections = parent_data.get("collections", []) if isinstance(parent_data, dict) else []

    if not isinstance(collections, list):
        return []

    seen: set[str] = set()
    out = []
    for c in collections:
        key = str(c).strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


