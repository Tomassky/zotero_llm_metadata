import json
import re
import random
import sys
import time
from typing import Tuple

try:
    import httpx
except Exception:
    print("Missing dependency: httpx. Install with `python3 -m pip install httpx`.", file=sys.stderr)
    raise

from file_extract import truncate_for_print


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------

def should_retry_status(status_code: int) -> bool:
    if status_code in (408, 409, 429):
        return True
    return status_code >= 500


def _sleep_and_retry(attempt: int, retries: int, last_err: str,
                     backoff: float, max_backoff: float, key: str) -> bool:
    """Sleep with jittered exponential backoff. Returns True if should retry."""
    if attempt > retries:
        return False
    delay = min(max_backoff, backoff * (2 ** (attempt - 1))) * random.uniform(0.8, 1.2)
    print(
        f"LLM RETRY {key}: attempt {attempt}/{retries + 1} failed ({last_err}); "
        f"retrying in {delay:.1f}s...",
        file=sys.stderr,
    )
    time.sleep(delay)
    return True


# ---------------------------------------------------------------------------
# LLM request
# ---------------------------------------------------------------------------

def request_llm_with_retry(
    client: httpx.Client,
    url: str,
    headers: dict,
    payload: dict,
    timeout: int,
    retries: int,
    backoff: float,
    max_backoff: float,
    key: str,
    debug: bool = False,
    max_print_chars: int = 4000,
) -> str:
    attempt = 0
    last_err = ""
    while attempt <= retries:
        attempt += 1
        try:
            req_timeout = httpx.Timeout(
                timeout=float(timeout),
                connect=min(20.0, float(timeout)),
                read=float(timeout),
                write=min(60.0, float(timeout)),
                pool=min(60.0, float(timeout)),
            )
            resp = client.post(url, headers=headers, json=payload, timeout=req_timeout)
            if debug:
                print(
                    f"LLM HTTP {key}: attempt {attempt}/{retries + 1}, "
                    f"status={resp.status_code}, url={url}",
                    file=sys.stderr,
                )
            if resp.status_code >= 400:
                if debug:
                    print(
                        f"\n=== LLM ERROR RESPONSE {key} ===\n"
                        f"{truncate_for_print(resp.text or '', max_print_chars)}",
                        file=sys.stderr,
                    )
                last_err = f"{resp.status_code} {resp.text}"
                if should_retry_status(resp.status_code) and _sleep_and_retry(
                    attempt, retries, last_err, backoff, max_backoff, key
                ):
                    continue
                break

            if debug:
                print(
                    f"\n=== LLM RAW RESPONSE {key} ===\n"
                    f"{truncate_for_print(resp.text or '', max_print_chars)}",
                    file=sys.stderr,
                )

            try:
                data = resp.json()
            except Exception as e:
                last_err = f"Non-JSON response: {e}; body={truncate_for_print(resp.text or '', max_print_chars)}"
                if _sleep_and_retry(attempt, retries, last_err, backoff, max_backoff, key):
                    continue
                break

            content = None
            if isinstance(data, dict):
                choices = data.get("choices")
                if isinstance(choices, list) and choices:
                    first = choices[0] if isinstance(choices[0], dict) else {}
                    msg = first.get("message") if isinstance(first, dict) else None
                    if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                        content = msg["content"]
                    if content is None and isinstance(first.get("text"), str):
                        content = first["text"]
            if isinstance(content, str):
                return content

            last_err = (
                "Unexpected LLM response schema. "
                f"body={truncate_for_print(resp.text or '', max_print_chars)}"
            )
            if _sleep_and_retry(attempt, retries, last_err, backoff, max_backoff, key):
                continue
            break

        except httpx.TimeoutException as e:
            last_err = f"Timeout: {e}"
            if debug:
                print(f"\n=== LLM TIMEOUT {key} ===\n{last_err}\nurl={url}", file=sys.stderr)
            if _sleep_and_retry(attempt, retries, last_err, backoff, max_backoff, key):
                continue
            break

        except Exception as e:
            last_err = str(e)
            if _sleep_and_retry(attempt, retries, last_err, backoff, max_backoff, key):
                continue
            break

    raise RuntimeError(last_err)


# ---------------------------------------------------------------------------
# JSON extraction from LLM response
# ---------------------------------------------------------------------------

def _sanitize_json_control_chars(text: str) -> str:
    """将 JSON 字符串值内的字面控制字符转义，修复 LLM 偶发的非法 JSON 输出。"""
    result = []
    in_string = False
    escape_next = False
    for ch in text:
        if escape_next:
            result.append(ch)
            escape_next = False
        elif ch == "\\" and in_string:
            result.append(ch)
            escape_next = True
        elif ch == '"':
            result.append(ch)
            in_string = not in_string
        elif in_string and ch == "\n":
            result.append("\\n")
        elif in_string and ch == "\r":
            result.append("\\r")
        elif in_string and ch == "\t":
            result.append("\\t")
        else:
            result.append(ch)
    return "".join(result)


def extract_json(text: str):
    text = text.strip()
    # 处理 LLM 偶尔输出的 markdown 代码围栏（```json ... ``` 或 ``` ... ```）
    fence = re.match(r"^```[a-zA-Z]*\n?(.*?)\n?```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    # 贪婪匹配，捕获从第一个 { 到最后一个 } 的完整 JSON 对象
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return match.group(0)
    return None


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def build_prompt(filename: str, content: str) -> Tuple[str, str]:
    system = (
        "You are a meticulous bibliographic cataloger for Zotero.\n"
        "Task: extract structured metadata ONLY from the given document text.\n"
        "Output must be ONE valid JSON object and nothing else.\n"
        "No markdown, no code fences, no explanations."
    )
    user = (
        "根据提供的文档内容提取 Zotero 元数据，严格输出一个 JSON 对象。\n\n"
        "字段与格式要求：\n"
        "1) 必须包含这些字段（不要增减字段）：\n"
        "itemType, title, creators, date, publicationTitle, publisher, place, DOI, url, abstractNote, language, tags\n"
        "2) creators 必须是数组；每个元素结构为 {\"firstName\":\"\",\"lastName\":\"\",\"creatorType\":\"author\"}\n"
        "3) tags 必须是字符串数组（例如 [\"机器学习\",\"文献综述\"]）\n"
        "4) 未知值：字符串字段用 \"\"，数组字段用 []，不要使用 null\n"
        "5) abstractNote 必须是中文摘要，且不少于 500 个中文字符\n"
        "6) date 优先使用 YYYY-MM-DD；若无法确定到日可用 YYYY-MM；再不确定用 YYYY\n"
        "7) DOI 仅保留 DOI 本体（例如 10.1000/xyz123），不要添加 https://doi.org/ 前缀\n"
        "8) 如果文档证据不足，不要臆造具体人名、期刊名、DOI、URL\n\n"
        "itemType 选择建议（按最匹配）：\n"
        "- journalArticle, conferencePaper, book, bookSection, thesis, report, preprint, document\n\n"
        "输出模板（字段名必须一致）：\n"
        "{\n"
        "  \"itemType\": \"\",\n"
        "  \"title\": \"\",\n"
        "  \"creators\": [],\n"
        "  \"date\": \"\",\n"
        "  \"publicationTitle\": \"\",\n"
        "  \"publisher\": \"\",\n"
        "  \"place\": \"\",\n"
        "  \"DOI\": \"\",\n"
        "  \"url\": \"\",\n"
        "  \"abstractNote\": \"\",\n"
        "  \"language\": \"\",\n"
        "  \"tags\": []\n"
        "}\n\n"
        f"文件名（可作为弱线索）: {filename}\n"
        "文档内容开始：\n"
        "<<<DOCUMENT>>>\n"
        f"{content}\n"
        "<<<END_DOCUMENT>>>\n"
    )
    return system, user


def build_abstract_prompt(item_key: str, evidence_text: str) -> Tuple[str, str]:
    system = (
        "You are a meticulous bibliographic cataloger for Zotero.\n"
        "Task: generate abstractNote ONLY from the provided evidence.\n"
        "Output must be ONE valid JSON object and nothing else.\n"
        "No markdown, no code fences, no explanations."
    )
    user = (
        "根据提供的文献证据，为该 Zotero 条目生成 abstractNote，严格输出一个 JSON 对象。\n\n"
        "字段与格式要求：\n"
        "1) 必须包含且仅包含这三个字段：abstractNote、confidence、insufficient_evidence\n"
        "2) abstractNote：必须是中文摘要，且不少于 500 个中文字符\n"
        "3) confidence：整数 0–100，表示对生成摘要的把握程度\n"
        "4) insufficient_evidence：布尔值；以下情况须设为 true，同时将 abstractNote 置为空字符串：\n"
        "   - 证据不足以生成可靠摘要\n"
        "   - attachment_fulltext 的内容与条目标题/作者明显属于不同文献（如书籍条目的全文是无关网页讨论）\n"
        "5) 不得臆造证据中未出现的事实、人名、数据\n\n"
        "输出模板（字段名必须一致）：\n"
        "{\"abstractNote\":\"\",\"confidence\":0,\"insufficient_evidence\":false}\n\n"
        f"条目 key: {item_key}\n"
        "文献证据开始：\n"
        "<<<EVIDENCE>>>\n"
        f"{evidence_text}\n"
        "<<<END_EVIDENCE>>>\n"
    )
    return system, user


def build_evidence_text(item_data: dict, attachment_fulltext: str, max_fulltext_chars: int) -> str:
    from zotero_api import format_creators
    tags = item_data.get("tags", [])
    tag_text = ""
    if isinstance(tags, list):
        tag_text = ", ".join(str(t.get("tag", "")).strip() for t in tags if isinstance(t, dict))
    lines = [
        f"itemType: {item_data.get('itemType', '')}",
        f"title: {item_data.get('title', '')}",
        f"date: {item_data.get('date', '')}",
        f"publicationTitle: {item_data.get('publicationTitle', '')}",
        f"publisher: {item_data.get('publisher', '')}",
        f"url: {item_data.get('url', '')}",
        f"DOI: {item_data.get('DOI', '')}",
        f"creators: {format_creators(item_data.get('creators', []))}",
        f"tags: {tag_text}",
        f"extra: {item_data.get('extra', '')}",
        f"note: {item_data.get('note', '')}",
    ]
    fulltext = (attachment_fulltext or "").strip()
    if max_fulltext_chars > 0 and len(fulltext) > max_fulltext_chars:
        fulltext = fulltext[:max_fulltext_chars]
    if fulltext:
        lines.append("attachment_fulltext:")
        lines.append(fulltext)
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Abstract generation
# ---------------------------------------------------------------------------

def generate_abstract_for_item(
    client: httpx.Client,
    item_key: str,
    evidence_text: str,
    llm_base: str,
    model: str,
    api_key: str,
    timeout: int,
    retries: int,
    backoff: float,
    max_backoff: float,
    max_output_tokens: int,
    debug: bool,
) -> dict:
    from zotero_api import join_url
    system, user = build_abstract_prompt(item_key=item_key, evidence_text=evidence_text)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": max_output_tokens,
    }
    raw = request_llm_with_retry(
        client=client,
        url=join_url(llm_base, "chat/completions"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        payload=payload,
        timeout=timeout,
        retries=retries,
        backoff=backoff,
        max_backoff=max_backoff,
        key=item_key,
        debug=debug,
    )
    j = extract_json(raw)
    if not j:
        return {"abstractNote": "", "confidence": 0, "insufficient_evidence": True,
                "error": "No JSON found in LLM response", "raw_response": raw}
    try:
        obj = json.loads(j)
    except Exception:
        # LLM 有时在字符串值内输出字面控制字符（如真实换行），尝试转义后重试
        try:
            obj = json.loads(_sanitize_json_control_chars(j))
        except Exception:
            return {"abstractNote": "", "confidence": 0, "insufficient_evidence": True,
                    "error": "Invalid JSON in LLM response", "raw_response": raw}
    abstract = obj.get("abstractNote", "")
    if not isinstance(abstract, str):
        abstract = ""
    try:
        confidence = float(obj.get("confidence", 0))
    except Exception:
        confidence = 0.0
    return {
        "abstractNote": abstract.strip(),
        "confidence": confidence,
        "insufficient_evidence": bool(obj.get("insufficient_evidence", False)),
        "raw_response": raw,
    }


# ---------------------------------------------------------------------------
# Image text extraction via VL model
# ---------------------------------------------------------------------------

def build_image_extract_prompt() -> str:
    return (
        "根据提供的图片，提取并描述其中的全部内容，严格输出一个 JSON 对象。\n\n"
        "字段与格式要求：\n"
        "1) 必须包含且仅包含这两个字段：image_text、confidence\n"
        "2) image_text：必须是中文，且不少于 500 个中文字符，内容要求：\n"
        "   - 如有文字，完整转录所有可见文字；\n"
        "   - 如有图表，描述标题、坐标轴标签、图例及数据趋势；\n"
        "   - 如有公式，用文字或 LaTeX 表达；\n"
        "   - 如有流程图/示意图，按顺序描述各节点与连接关系；\n"
        "   - 综合说明图片的整体主题与信息要点。\n"
        "3) confidence：整数 0–100，表示对图片内容识别的把握程度\n"
        "4) 若图片无法识别或内容过少，image_text 置为空字符串，confidence 置为 0\n\n"
        "输出模板（字段名必须一致）：\n"
        "{\"image_text\":\"\",\"confidence\":0}\n"
    )


def extract_text_from_image(
    client,
    image_b64: str,
    mime_type: str,
    llm_base: str,
    vl_model: str,
    api_key: str,
    timeout: int,
    retries: int,
    backoff: float,
    max_backoff: float,
    debug: bool = False,
) -> str:
    """调用 VL 模型将图片转换为文字描述，返回 image_text 字符串；失败时返回空字符串。"""
    from zotero_api import join_url
    data_url = f"data:{mime_type};base64,{image_b64}"
    payload = {
        "model": vl_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a meticulous document image analyst for Zotero.\n"
                    "Task: extract and describe ALL content from the given image.\n"
                    "Output must be ONE valid JSON object and nothing else.\n"
                    "No markdown, no code fences, no explanations."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": build_image_extract_prompt()},
                ],
            },
        ],
        "temperature": 0.1,
        "max_tokens": 2000,
    }
    raw = request_llm_with_retry(
        client=client,
        url=join_url(llm_base, "chat/completions"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        payload=payload,
        timeout=timeout,
        retries=retries,
        backoff=backoff,
        max_backoff=max_backoff,
        key="<image>",
        debug=debug,
    )
    j = extract_json(raw)
    if not j:
        return ""
    try:
        obj = json.loads(j)
    except Exception:
        try:
            obj = json.loads(_sanitize_json_control_chars(j))
        except Exception:
            return ""
    return str(obj.get("image_text", "")).strip()
