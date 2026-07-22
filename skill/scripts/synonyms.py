"""Tag synonym normalization — vendored copy for the self-contained skill.

⚠️  SINGLE SOURCE OF TRUTH lives in the pipeline at
    ``src/zotero_llm_metadata/graph/builder.py`` (SYNONYM_MAP / normalize_tag /
    resolve_synonym). This file is a deliberate copy so the skill has **no**
    Python import dependency on the repo — the graph is built with the same
    mapping, so query-time normalization must match it. If you edit the map in
    one place, mirror the change here (and vice-versa).
"""
from __future__ import annotations

import re

# Maps variant normalized tags to their canonical form.
# Canonical = the most common Chinese variant for each concept group.
SYNONYM_MAP: dict[str, str] = {
    # Bilingual pairs (English → Chinese canonical)
    "penetration-testing": "渗透测试",
    "red-teaming": "红队技术",
    "red-team": "红队技术",
    "red-team-operations": "红队技术",
    "large-language-models": "大语言模型",
    "ai-agents": "ai智能体",
    "ai-agent": "ai智能体",
    "computer-science---artificial-intelligence": "人工智能",
    "model-context-protocol": "模型上下文协议",
    "mcp协议": "模型上下文协议",
    "rag": "检索增强生成",
    "retrieval-augmented-generation": "检索增强生成",
    "malware-analysis": "恶意软件分析",
    "reverse-engineering": "逆向工程",
    "privilege-escalation": "权限提升",
    "network-security": "网络安全",
    "security": "网络安全",
    "offensive-security": "渗透测试",
    "cybersecurity": "网络安全",
    "llm": "大语言模型",
    "llm-integration": "大语言模型集成",
    "nlp": "自然语言处理",
    "ai-assisted-development": "ai辅助开发",
    "ai-assisted-programming": "ai辅助编程",
    "developer-productivity": "开发者工具",
    "process-injection": "进程注入",
    "defense-evasion": "免杀技术",
    "evasion": "免杀技术",
    "post-exploitation": "后渗透",
    "credential-access": "凭证窃取",
    "credential-dumping": "凭证窃取",
    "malware-development": "恶意软件分析",
    "edr-evasion": "免杀技术",
    "context-management": "上下文工程",
    "context-optimization": "上下文工程",
    "information-retrieval": "检索增强生成",
    "multi-agent-systems": "多智能体协作",
    "multi-agent-system": "多智能体协作",
    "autonomous-agents": "ai智能体",
    "agent-orchestration": "智能体编排",
    "agentic-workflows": "智能体工作流",
    "vulnerability-assessment": "漏洞利用",
    "web-security": "web安全",
    "threat-intelligence": "威胁情报",
    "kernel-security": "系统安全",
    # Chinese near-synonyms (variant → canonical)
    "免杀": "免杀技术",
    "免杀技术": "免杀技术",
    "红队行动": "红队技术",
    "红队战术": "红队技术",
    "红队工具": "红队技术",
    "红队": "红队技术",
    "红队对抗": "红队技术",
    "红队测试": "红队技术",
    "红蓝对抗": "渗透测试",
    "攻防": "渗透测试",
    "大语言模型应用": "大语言模型",
    "大语言模型集成": "大语言模型",
    "大模型": "大语言模型",
    "大模型应用": "大语言模型",
    "大语言模型安全": "大语言模型",
    "ai代理": "ai智能体",
    "ai智能体架构": "ai智能体",
    "智能体架构": "ai智能体",
    "智能体工作流": "ai智能体",
    "智能体编排": "ai智能体",
    "智能体": "ai智能体",
    "多代理协作": "多智能体协作",
    "多智能体系统": "多智能体协作",
    "多智能体协同": "多智能体协作",
    "多智能体架构": "多智能体协作",
    "多智能体": "多智能体协作",
    "ai辅助编程": "ai辅助开发",
    "ai编程助手": "ai辅助开发",
    "ai辅助安全": "ai辅助开发",
    "ai编程辅助": "ai辅助开发",
    "上下文管理": "上下文工程",
    "上下文优化": "上下文工程",
    "云安全": "网络安全",
    "靓仔云安全": "网络安全",
    "信息安全": "网络安全",
    "安全": "网络安全",
    "进程注入": "进程注入",
    "内存注入": "进程注入",
    "内存执行": "进程注入",
    "内存加载": "进程注入",
    "恶意代码分析": "恶意软件分析",
    "恶意软件": "恶意软件分析",
    "恶意代码": "恶意软件分析",
    "恶意软件开发": "恶意软件分析",
    "应急响应": "应急响应",
    "安全事件响应": "应急响应",
    "后渗透技术": "后渗透",
    "漏洞挖掘": "漏洞利用",
    "漏洞分析": "漏洞利用",
    "漏洞研究": "漏洞利用",
    "漏洞利用开发": "漏洞利用",
    "漏洞检测": "漏洞利用",
    "提权": "权限提升",
    "本地权限提升": "权限提升",
    "windows安全": "系统安全",
    "安全运营": "安全运营",
    "安全研究": "安全研究",
    "webshell": "webshell",
    "模型上下文协议": "模型上下文协议",
}


def normalize_tag(tag: str) -> str:
    """Normalize a tag for deduplication."""
    t = tag.strip().lower()
    if t.startswith("#"):
        t = t[1:]
    t = re.sub(r"[/\-_]+", "-", t)
    t = re.sub(r"\s+", "-", t)
    return t.strip("-")


def resolve_synonym(tag: str) -> str:
    """Resolve a normalized tag to its canonical synonym form."""
    return SYNONYM_MAP.get(tag, tag)
