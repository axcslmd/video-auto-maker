#!/usr/bin/env python3
"""语言合同：在付费视频调用前阻止台词悄悄切换语言。

提示词可以包含少量产品名、模型名和技术缩写，但真正的对白必须服从
storyboard_plan/audio.language。这个模块只检查对白字段，不把 PPT 原生文字
或品牌名误判成配音语言。
"""
import re


_ZH_ALIASES = {
    "zh", "zh-cn", "zh-hans", "zh_cn", "中文", "中文普通话", "普通话", "mandarin",
    "cmn", "国语", "简体中文", "粤语", "中文粤语", "yue",
}
_EN_ALIASES = {"en", "en-us", "en-gb", "en_us", "english", "英文", "英语"}

# These tokens are common in Chinese business scripts and do not constitute an
# English sentence. Keep this list deliberately small and domain-neutral.
_COMMON_LATIN_TOKENS = {
    "AI", "API", "B2B", "B2C", "CRM", "ERP", "HR", "IT", "KPI", "OKR", "PPT",
    "PDF", "SaaS", "SKU", "UI", "UX", "URL", "OS", "Basicware", "Remotion",
    "HyperFrames", "Kling", "Seedance", "GPT", "RPA", "Demo",
}


def normalize_language(value, default=None):
    if value is None:
        return default
    raw = str(value).strip()
    if not raw:
        return default
    lowered = raw.lower().replace("_", "-")
    if lowered in _ZH_ALIASES:
        return "zh-CN"
    if lowered in _EN_ALIASES:
        return "en-US"
    return raw


def is_chinese_language(value):
    normalized = normalize_language(value)
    return normalized is not None and normalized.lower() in {"zh-cn", "zh-hans", "cmn", "yue"}


def is_english_language(value):
    normalized = normalize_language(value)
    return normalized is not None and normalized.lower() in {"en-us", "en-gb", "english"}


def _latin_tokens(text):
    return re.findall(r"[A-Za-z][A-Za-z0-9'_-]*", str(text or ""))


def dialogue_language_issues(dialogue, language):
    """Return human-readable issues; an empty list means no detectable mismatch."""
    text = str(dialogue or "").strip()
    if not text or not language:
        return []
    if not is_chinese_language(language):
        return []
    tokens = _latin_tokens(text)
    meaningful = [token for token in tokens
                  if token not in _COMMON_LATIN_TOKENS
                  and token.upper() not in _COMMON_LATIN_TOKENS]
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", text))
    # A Chinese sentence may contain product/model names. A dialogue with no
    # Chinese characters and multiple ordinary Latin words is an English line.
    if meaningful and cjk_count == 0 and len(meaningful) >= 2:
        return ["对白没有中文字符，疑似误写为英文"]
    if len(meaningful) >= 3 and cjk_count < len(meaningful) * 2:
        return ["对白中的普通英文词占比过高，疑似语言漂移"]
    return []


def ensure_dialogue_language(dialogue, language, context="segment"):
    issues = dialogue_language_issues(dialogue, language)
    if issues:
        raise ValueError(
            "DIALOGUE_LANGUAGE_MISMATCH: %s 的对白必须使用%s；%s；原对白=%r" %
            (context, "中文" if is_chinese_language(language) else str(language),
             "；".join(issues), str(dialogue or "")))
    return True


def language_instruction(language):
    normalized = normalize_language(language)
    if is_chinese_language(normalized):
        return "本段声音必须使用中文（普通话/已确认中文口音）朗读确认台词，禁止自动改成英文；品牌名、AI、API等缩写按中文语境自然读出。"
    if is_english_language(normalized):
        return "本段声音必须使用英文朗读确认台词，禁止自动切换到中文。"
    return "声音语言以已确认的 audio_contract.language 和 dialogue 为准，禁止自行改写台词或切换语言。"

