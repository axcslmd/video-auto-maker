#!/usr/bin/env python3
"""Small user-facing helpers shared by command-line workflows."""
import os


def friendly_error(error):
    """Translate common service failures into an actionable client message."""
    text = str(error or "").strip()
    lower = text.lower()
    if "no api key" in lower or "key onboarding" in lower or "session_required" in lower:
        return "还没有完成 BasicRouter 密钥设置。请通过宿主安全凭证输入提交一个以 sk- 开头的密钥；请勿粘贴到普通聊天中。我会先验证，再继续创作。"
    if "insufficient credit" in lower or "余额不足" in text or "credit" in lower:
        return "BasicRouter 额度不足，暂时无法继续生成。充值后重新运行这一阶段即可，不需要重新整理脚本。"
    if ("invalid" in lower and "key" in lower) or "密钥无效" in text or "401" in lower:
        return "BasicRouter 密钥无效或已过期。请通过宿主安全凭证输入提交新的 sk- 密钥；请勿粘贴到普通聊天中。"
    if "429" in lower or "rate" in lower or "限流" in text or "brratelimited" in lower:
        return "生成服务当前比较繁忙，系统已经自动重试。稍后重跑当前阶段即可，不会影响已保存的脚本和分镜。"
    if any(token in lower for token in ("timeout", "timed out", "network", "connection", "remote end closed")):
        return "网络刚才不太稳定，当前任务没有完成。请重试这一阶段，已保存的素材和确认结果会继续保留。"
    if "ocr_warning" in lower or "subtitle_detected" in lower:
        return "成片检测到疑似字幕或水印文字，暂不交付。建议重新生成该段，或确认接受检测到的文字后再合成。"
    return "这一步没有完成。已保留现有进度，请根据上面的具体阶段重新运行，不必从头开始。"


def absolute_path(path):
    return os.path.abspath(path) if path else None


def progress_hint(stage, *, current=None, total=None):
    if stage == "submit":
        return "正在提交生成任务，提交后会继续显示进度。"
    if stage == "poll":
        if current is not None and total:
            return "正在生成第 %s/%s 段，通常需要 1–3 分钟。" % (current, total)
        return "正在生成，通常需要 1–3 分钟；任务完成后会自动保存成片。"
    if stage == "download":
        return "生成已完成，正在下载并做文字残留检查。"
    return "正在处理，请稍候。"
