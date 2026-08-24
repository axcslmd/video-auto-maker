#!/usr/bin/env python3
"""BasicRouter API client — stdlib only (urllib), no pip needed.

Endpoints (base https://api.basicrouter.ai/api):
  POST /v1/chat/completions        chat / script / creative (OpenAI-compatible)
  POST /v1/image-generations       ASYNC image -> data.taskId
  GET  /v1/image-generations/{id}  poll -> data.images
  POST /v1/video-generations       ASYNC video -> data.taskId
  GET  /v1/video-generations/{id}  poll -> data.{status,videoUrl,lastFrameUrl}
  GET  /employee/models            model list (no auth), category=video|image|text
Envelope: {code,message,data}; code 200 ok, -1 insufficient credit.
"""
import os
import json
import socket
import ssl
import shutil
import subprocess
import time
import random
import base64
import hashlib
import tempfile
import uuid
import urllib.request
import urllib.error
import urllib.parse
import unicodedata
import ipaddress
import re

import api_endpoint_setup

# BasicRouter is the public service contract for this package. Do not infer a
# provider endpoint from a rendered/aliased documentation page. The historical
# BasicRouter API paths below are intentionally the default; any alternate
# gateway must be explicitly selected by the operator.
BASICROUTER_BASE_URL = api_endpoint_setup.DEFAULT_BASE_URL
# Deprecated compatibility export. Each request resolves its media route at
# call time so a value saved by the initialization dialog applies immediately.
BASE_URL = BASICROUTER_BASE_URL
# Only the BasicRouter contract is supported. Keep the name for internal
# compatibility, but do not allow an environment variable to select another
# provider or domain.
API_MODE = "legacy"

# 限流/瞬时错误重试配置：429(限流) 和 5xx(服务端瞬时) 用指数退避重试。
# 网关（对接上游 LLM）会 429，客户端必须能扛住而不是直接失败。
MAX_RETRIES = 4          # 首次 + 最多 4 次重试 = 5 次尝试
BASE_BACKOFF = 2.0       # 退避基数秒；第 n 次退避 ≈ BASE_BACKOFF * 2**(n-1) + 抖动
MAX_BACKOFF = 30.0       # 单次退避上限
RETRY_STATUS = {429, 500, 502, 503, 504}
_AUTH_REJECTION_HANDLER = None


class BRError(Exception):
    def __init__(self, message, *, payload=None, http_status=None, request_id=None):
        super().__init__(message)
        self.payload = payload
        self.http_status = http_status
        self.request_id = request_id


class BRRateLimited(BRError):
    """429 且重试耗尽时抛出，便于调用方区分限流与其它错误。"""
    pass


class BRVideoReferencePrivacyError(BRError):
    """Seedance rejected a real-person reference image for privacy reasons."""


def set_auth_rejection_handler(handler):
    """Register a credential-store callback without coupling this client to it."""
    global _AUTH_REJECTION_HANDLER
    if handler is not None and not callable(handler):
        raise TypeError("AUTH_REJECTION_HANDLER_MUST_BE_CALLABLE")
    _AUTH_REJECTION_HANDLER = handler


def is_authentication_error(value):
    """Match explicit credential rejection, not generic policy/safety 403s."""
    status = value.http_status if isinstance(value, BRError) else None
    if status == 401:
        return True
    text = unicodedata.normalize(
        "NFKC", " ".join(_error_texts(
            value.payload if isinstance(value, BRError) else value)) +
        " " + str(value)).casefold()
    auth_terms = (
        "invalid api key", "invalid_api_key", "unauthorized",
        "authentication failed", "invalid bearer", "密钥无效", "密钥已失效",
    )
    return any(term in text for term in auth_terms)


def _notify_auth_rejection(api_key, error):
    """Revoke cached readiness after a proven auth rejection; never mask it."""
    if not api_key or not is_authentication_error(error):
        return False
    handler = _AUTH_REJECTION_HANDLER
    if handler is None:
        return False
    try:
        handler(api_key)
    except (OSError, ValueError, TypeError, RuntimeError):
        # The original HTTP/API error remains authoritative.  A cache cleanup
        # failure must not convert it into an unrelated exception.
        return False
    return True


def _error_texts(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _error_texts(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _error_texts(item)
    elif value is not None:
        yield str(value)


def is_video_reference_privacy_error(value):
    """Match only reference-image + real-person + privacy rejection errors.

    Requiring all three groups prevents generic safety, authentication, billing,
    URL-download and parameter errors from being rerouted to another model.
    """
    payload = value.payload if isinstance(value, BRError) and value.payload is not None else value
    text = " ".join(_error_texts(payload))
    if isinstance(value, BaseException):
        text += " " + str(value)
    text = unicodedata.normalize("NFKC", text).lower()
    reference_terms = (
        "reference image", "input image", "uploaded image", "image reference",
        "reference photo", "参考图", "输入图片", "上传图片", "上传的图", "引用图",
    )
    person_terms = (
        "real person", "real human", "human face", "identifiable person", "portrait",
        "photo of a person", "真实人物", "真人", "人脸", "人物肖像", "可识别人物",
    )
    rejection_terms = (
        "privacy", "not supported", "rejected", "refused", "prohibited", "not allowed",
        "policy restriction", "隐私", "拒绝", "不支持", "不允许", "禁止", "检测未通过",
    )
    return (any(term in text for term in reference_terms)
            and any(term in text for term in person_terms)
            and any(term in text for term in rejection_terms))


def is_insufficient_credit(value):
    """Classify billing exhaustion so it is never treated as a transient 5xx."""
    payload = value.payload if isinstance(value, BRError) and value.payload is not None else value
    text = unicodedata.normalize(
        "NFKC", " ".join(_error_texts(payload)) + " " + str(value)).lower()
    return ("insufficient credit" in text or "insufficient_credits" in text or
            "余额不足" in text or "额度不足" in text or
            "credit not enough" in text)


def is_video_model_not_found(value):
    """Return true for provider errors that mean the submitted model ID is invalid."""
    payload = value.payload if isinstance(value, BRError) and value.payload is not None else value
    text = unicodedata.normalize("NFKC", " ".join(_error_texts(payload)) + " " + str(value)).lower()
    return (isinstance(value, BRError) and value.http_status == 400 and
            ("model not found" in text or "model_not_found" in text or
             "unknown model" in text or "模型不存在" in text or "模型未找到" in text))


def video_task_error(info, prefix="video task failed"):
    message = info.get("message") if isinstance(info, dict) else None
    error = BRError("%s: %s" % (prefix, message or info), payload=info)
    if is_video_reference_privacy_error(error):
        return BRVideoReferencePrivacyError(str(error), payload=info)
    return error


def _backoff_seconds(attempt, retry_after=None):
    """第 attempt 次重试(从1起)的等待秒数。优先服务端 Retry-After，否则指数退避+抖动。"""
    if retry_after is not None:
        try:
            return min(float(retry_after), MAX_BACKOFF)
        except (TypeError, ValueError):
            pass
    exp = BASE_BACKOFF * (2 ** (attempt - 1))
    return min(exp, MAX_BACKOFF) + random.uniform(0, 1.0)  # 抖动避免雷同重试


def _is_tls_runtime_error(error):
    """Identify failures that happen before an HTTPS request can be sent."""
    reason = getattr(error, "reason", error)
    text = str(reason).lower()
    return (isinstance(reason, ssl.SSLError) or
            "ssl" in text or "tls" in text or
            "eof occurred in violation of protocol" in text)


def _curl_json_request(method, url, headers, data, timeout, api_key=None):
    """Use system curl when the host Python TLS runtime cannot handshake.

    Secrets never appear in argv or project files. Curl receives them through
    a mode-0600 temporary config which is deleted immediately after the call.
    """
    curl = shutil.which("curl")
    if not curl:
        raise BRError("TLS_RUNTIME_INCOMPATIBLE: system curl unavailable")
    temp_dir = tempfile.mkdtemp(prefix="br-curl-")
    os.chmod(temp_dir, 0o700)
    config_path = os.path.join(temp_dir, "request.conf")
    request_path = os.path.join(temp_dir, "request.json")
    response_path = os.path.join(temp_dir, "response.json")
    header_path = os.path.join(temp_dir, "response.headers")
    try:
        if data is not None:
            with open(request_path, "wb") as handle:
                handle.write(data)
            os.chmod(request_path, 0o600)
        config_lines = [
            "silent",
            "show-error",
            "location",
            'request = "%s"' % method.replace('"', ''),
            'url = "%s"' % url.replace('"', '%22'),
            "connect-timeout = 30",
            "max-time = %d" % max(1, int(timeout)),
            'dump-header = "%s"' % header_path,
        ]
        for name, value in headers.items():
            clean_name = str(name).replace('"', '')
            clean_value = str(value).replace('"', '\\"').replace("\n", "").replace("\r", "")
            config_lines.append('header = "%s: %s"' % (clean_name, clean_value))
        if data is not None:
            config_lines.append('data-binary = "@%s"' % request_path)
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(config_lines) + "\n")
        os.chmod(config_path, 0o600)
        completed = subprocess.run(
            [curl, "--config", config_path, "--output", response_path,
             "--write-out", "%{http_code}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout + 35)
        if completed.returncode != 0:
            error = completed.stderr.decode("utf-8", "replace")[:400]
            raise BRError("network error (curl TLS fallback): %s" % error)
        status_text = completed.stdout.decode("ascii", "replace").strip()
        try:
            status = int(status_text[-3:])
        except ValueError:
            raise BRError("curl TLS fallback returned invalid HTTP status")
        with open(response_path, "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
        response_headers = {}
        if os.path.exists(header_path):
            with open(header_path, encoding="iso-8859-1") as handle:
                for line in handle:
                    if ":" in line:
                        key, value = line.split(":", 1)
                        response_headers[key.strip().lower()] = value.strip()
        if status < 200 or status >= 300:
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                payload = raw[:400]
            if is_insufficient_credit(payload):
                raise BRError("Insufficient credit (余额不足): %s" % raw[:300],
                              payload=payload, http_status=status,
                              request_id=response_headers.get("x-request-id"))
            if status == 429:
                raise BRRateLimited("限流(429): %s" % raw[:300], payload=payload,
                                    http_status=status)
            error = BRError("HTTP %s: %s" % (status, raw[:400]), payload=payload,
                            http_status=status,
                            request_id=response_headers.get("x-request-id"))
            _notify_auth_rejection(api_key, error)
            if is_video_reference_privacy_error(error):
                raise BRVideoReferencePrivacyError(str(error), payload=payload,
                                                   http_status=status,
                                                   request_id=error.request_id)
            raise error
        try:
            result = json.loads(raw)
            if (api_key and isinstance(result, dict) and
                    result.get("code") not in (None, 200)):
                _notify_auth_rejection(
                    api_key, BRError("API authentication rejected", payload=result))
            return result
        except json.JSONDecodeError:
            raise BRError("non-JSON response: " + raw[:400])
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _api_kind_for_request(path, query=None):
    """Select the configured text, image, or video proxy for one request."""
    clean_path = str(path or "").lower()
    if clean_path.startswith(("/v1/image", "/ai/createimage")):
        return "image"
    if clean_path.startswith(("/v1/video", "/ai/createvideo")):
        return "video"
    if clean_path.startswith("/employee/models"):
        category = str((query or {}).get("category") or "text").lower()
        return category if category in {"text", "image", "video"} else "text"
    return "text"


def _request(method, path, api_key=None, body=None, query=None, timeout=120,
             max_retries=MAX_RETRIES, on_retry=None, idempotency_key=None,
             api_kind=None):
    """发请求，安全方法或带幂等键的请求会对瞬时故障退避重试。

    on_retry(attempt, wait, reason) 可选回调，用于打印「限流中，Ns 后重试」。
    非幂等请求不带 idempotency_key 时绝不自动重试，避免重复付费创建。
    重试耗尽：429 抛 BRRateLimited，其它抛 BRError。4xx(非429) 不重试直接抛。
    """
    method = method.upper()
    api_kind = api_kind or _api_kind_for_request(path, query)
    url = api_endpoint_setup.base_url(api_kind) + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    if idempotency_key:
        headers["Idempotency-Key"] = str(idempotency_key)
    can_retry = method in {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"} or bool(idempotency_key)

    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            try:
                result = json.loads(raw)
                if (api_key and isinstance(result, dict) and
                        result.get("code") not in (None, 200)):
                    _notify_auth_rejection(
                        api_key, BRError(
                            "API authentication rejected", payload=result))
                return result
            except json.JSONDecodeError:
                raise BRError("non-JSON response: " + raw[:400])
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                payload = raw[:400]
            if is_insufficient_credit(payload):
                raise BRError("Insufficient credit (余额不足): %s" % raw[:300],
                              payload=payload, http_status=e.code,
                              request_id=(e.headers.get("X-Request-Id")
                                          if e.headers else None))
            # 可重试状态码：退避后重试
            if can_retry and e.code in RETRY_STATUS and attempt < max_retries:
                attempt += 1
                retry_after = e.headers.get("Retry-After") if e.headers else None
                wait = _backoff_seconds(attempt, retry_after)
                if on_retry:
                    on_retry(attempt, wait, "HTTP %s" % e.code)
                time.sleep(wait)
                continue
            if e.code == 429:
                raise BRRateLimited(
                    "限流(429)重试 %d 次仍失败: %s" % (attempt, raw[:300]))
            request_id = e.headers.get("X-Request-Id") if e.headers else None
            error = BRError("HTTP %s: %s" % (e.code, raw[:400]), payload=payload,
                            http_status=e.code, request_id=request_id)
            _notify_auth_rejection(api_key, error)
            if is_video_reference_privacy_error(error):
                raise BRVideoReferencePrivacyError(str(error), payload=payload,
                                                   http_status=e.code, request_id=request_id)
            raise error
        except (urllib.error.URLError, ConnectionError, EOFError, TimeoutError,
                socket.timeout) as e:
            # 网络瞬时错误也重试（超时/连接重置等）
            reason = getattr(e, "reason", e)
            if _is_tls_runtime_error(e):
                return _curl_json_request(
                    method, url, headers, data, timeout, api_key=api_key)
            if can_retry and attempt < max_retries:
                attempt += 1
                wait = _backoff_seconds(attempt)
                if on_retry:
                    on_retry(attempt, wait, "network: %s" % reason)
                time.sleep(wait)
                continue
            raise BRError("network error: %s" % reason)


def _unwrap(resp):
    """Return data from {code,message,data} envelope, raising on business error."""
    if not isinstance(resp, dict):
        return resp
    if "code" in resp and resp.get("code") != 200:
        msg = resp.get("message") or resp.get("data") or "unknown error"
        if resp.get("code") == -1:
            raise BRError("Insufficient credit (余额不足): %s" % msg)
        error = BRError("API error code=%s: %s" % (resp.get("code"), msg), payload=resp)
        if is_video_reference_privacy_error(error):
            raise BRVideoReferencePrivacyError(str(error), payload=resp)
        raise error
    return resp.get("data", resp)


# ---------- public API ----------

def list_models(category=None):
    """Discover models through the documented /v1 endpoints."""
    if API_MODE == "legacy":
        q = {"category": category} if category else None
        return _unwrap(_request("GET", "/employee/models", query=q, timeout=30))
    if category == "image":
        return list_image_models()
    if category == "video":
        return list_video_models()
    data = _unwrap(_request("GET", "/v1/models", timeout=30))
    return data.get("data", data) if isinstance(data, dict) else data


def list_video_models(api_key=None):
    if API_MODE == "legacy":
        return list_models("video")
    data = _unwrap(_request("GET", "/v1/video-models", api_key=api_key, timeout=30))
    return data.get("data", data) if isinstance(data, dict) else data


# 视觉多模态模型偏好序（跨脚本共享，避免 video_reverse.py/subtitle_overlay.py/
# asset_prep.py 各写一份重复逻辑）。实时从 /employee/models 挑 online 且
# multimodelTypes 含 "image" 的 modelId；偏好命中就用，否则退回列表里任一在线
# 视觉模型，最后兜底固定值。不硬编码具体网关是否上线，一律以实时列表为准。
VISION_MODEL_PREFERENCE = [
    "kimi-k3", "qwen3-vl-plus", "qwen3-vl-flash", "qwen3.6-plus", "qwen3.7-plus",
    "gemini-3-flash-preview", "gpt-5.5", "minimax-m3",
]
VISION_MODEL_FALLBACK = "kimi-k3"


def list_vision_models():
    """返回当前 online 且支持图片输入（multimodelTypes 含 image）的 modelId 集合。"""
    ids = set()
    try:
        for x in (list_models("text") or []):
            if API_MODE == "legacy" and not x.get("online"):
                continue
            mid = x.get("modelId") or x.get("modelName") or x.get("id")
            if not mid:
                continue
            try:
                types = x.get("multimodelTypes")
                if types is None:
                    types = x.get("input_modalities") or []
                if isinstance(types, str):
                    types = json.loads(types or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                types = []
            if "image" in types:
                ids.add(mid)
    except (BRError, OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return ids


def pick_vision_model():
    """实时选一个在线的视觉多模态模型：先按偏好序命中，否则取任一在线视觉模型，最后兜底。"""
    vision = list_vision_models()
    if vision:
        for m in VISION_MODEL_PREFERENCE:
            if m in vision:
                return m
        return sorted(vision)[0]  # 偏好都没命中，取任一在线视觉模型（稳定排序）
    return VISION_MODEL_FALLBACK


def analyze_image(api_key, image_path_or_url, question, *, model=None,
                   system_prompt=None, timeout=600, host_timeout=600):
    """用 BasicRouter 在线视觉模型分析一张图片（走客户自己的 key，不依赖本地 vision 工具）。

    为什么要有这个函数：宿主 Agent 自带的视觉工具通常依赖宿主侧单独配置的视觉
    模型供应商；本项目客户机可能完全没配置该能力，但客户已经配好了 BasicRouter key
    （本项目所有生成/分析都应该走这把 key，见 AGENTS.md 分工铁律）。本函数走同一套
    /v1/chat/completions 多模态
    协议（input_text + input_image），跟 video_reverse.py 的逆向分析共用同一套模型选型。

    image_path_or_url: 本地文件路径或已可访问的 URL；本地路径会先经 to_image_ref()
        上传成 hosted https URL（BasicRouter 多模态要求 image_url 是可访问地址）。
    返回: 模型的文字分析结果（string）。
    """
    model = model or pick_vision_model()
    try:
        image_url = to_image_ref(
            image_path_or_url, api_key=api_key, prefer_hosted=True,
            host_timeout=host_timeout)
    except (BRError, OSError, ValueError, TypeError) as e:
        raise BRError("图片上传失败：托管超时或失败，已保持同一托管路径，未切换或重绘: %s" % e) from e

    content = [
        {"type": "input_text", "text": question},
        {"type": "input_image", "image_url": image_url},
    ]
    msgs = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    msgs.append({"role": "user", "content": content})

    # 重推理视觉模型非流式易被网关长连接断开，优先流式保活，失败再降级非流式。
    try:
        return chat_stream(api_key, msgs, model=model, timeout=timeout)
    except (urllib.error.URLError, ConnectionError, EOFError, TimeoutError,
            socket.timeout) as stream_error:
        # Only transport failures justify replaying a paid vision request. API
        # validation, safety, and credit failures must surface exactly once.
        return chat(api_key, msgs, model=model, timeout=timeout)
    except RuntimeError as stream_error:
        # Some SSE clients surface an interrupted response as RuntimeError
        # instead of a socket exception. Keep this narrow so business errors
        # such as credit/policy failures are never replayed.
        if "sse" not in str(stream_error).lower():
            raise
        return chat(api_key, msgs, model=model, timeout=timeout)


def validate_key(api_key):
    """Return ``(ok, message)`` using an authenticated low-cost chat ping.

    ``/employee/models`` is a public discovery endpoint and cannot prove that
    a supplied key is valid. It is only used to choose an online text model.
    """
    if not isinstance(api_key, str) or not api_key.strip().startswith("sk-"):
        return False, "密钥格式不正确，应以 sk- 开头"

    try:
        avail_raw = _request(
            "GET", "/employee/models" if API_MODE == "legacy" else "/v1/models",
            query={"category": "text"} if API_MODE == "legacy" else None,
            timeout=15)
    except (BRError, OSError, ValueError, TypeError):
        avail_raw = None

    _TEXT_PING_CANDIDATES = ["qwen3.6-plus", "qwen-plus", "qwen-turbo",
                              "gpt-4o-mini", "deepseek-chat"]
    ping_model = _TEXT_PING_CANDIDATES[0]
    if isinstance(avail_raw, (dict, list)):
        data = avail_raw if isinstance(avail_raw, list) else avail_raw.get("data", avail_raw)
        names = [(m.get("modelName") or m.get("modelId") or m.get("id") or "")
                 for m in (data or [])
                 if API_MODE != "legacy" or (m.get("online") and m.get("status"))]
        for candidate in _TEXT_PING_CANDIDATES:
            if candidate in names:
                ping_model = candidate
                break
    try:
        resp = _request(
            "POST", "/v1/chat/completions", api_key=api_key,
            body={"model": ping_model,
                  "messages": [{"role": "user", "content": "ping"}],
                  "max_tokens": 3},
            timeout=30)
        if isinstance(resp, dict) and resp.get("code") not in (None, 200):
            return False, resp.get("message") or "密钥无效或无权限"
        if isinstance(resp, dict) and ("choices" in resp or resp.get("code") == 200):
            _extract_chat_text(resp)
            return True, "ok"
        return False, "unexpected response"
    except (BRError, OSError, ValueError, TypeError) as e:
        return False, str(e)


def chat(api_key, messages, model="qwen3.6-plus", **kw):
    """Non-stream chat completion. Returns assistant text.

    NOTE: script/dialogue generation is done by the client's local host agent,
    NOT this function. Kept only as an optional fallback; do not use it for
    routine script writing (that would spend BasicRouter credits unnecessarily).
    """
    return _extract_chat_text(chat_completion(api_key, messages, model=model, **kw))


def chat_completion(api_key, messages, model="qwen3.6-plus", **kw):
    """Return the raw compatible chat response for structured/tool callers."""
    timeout = kw.pop("timeout", 120)
    body = {"model": model, "messages": messages}
    body.update(kw)
    return _request("POST", "/v1/chat/completions", api_key=api_key,
                    body=body, timeout=timeout)


def extract_tool_arguments(resp, tool_name=None):
    """Extract a function/tool arguments object from compatible responses."""
    data = _unwrap(resp)
    choices = data.get("choices") if isinstance(data, dict) else None
    if choices:
        message = (choices[0] or {}).get("message") or {}
        for call in message.get("tool_calls") or []:
            function = (call or {}).get("function") or {}
            if tool_name and function.get("name") != tool_name:
                continue
            arguments = function.get("arguments")
            if isinstance(arguments, dict):
                return arguments
            if isinstance(arguments, str):
                return json.loads(arguments)
    if isinstance(data, dict):
        message = data.get("message") or {}
        for call in message.get("tool_calls") or []:
            function = (call or {}).get("function") or {}
            if tool_name and function.get("name") != tool_name:
                continue
            arguments = function.get("arguments")
            return json.loads(arguments) if isinstance(arguments, str) else arguments
        content_blocks = data.get("content") or message.get("content") or []
        if isinstance(content_blocks, list):
            for block in content_blocks:
                if not isinstance(block, dict) or block.get("type") not in {"tool_use", "function_call"}:
                    continue
                if tool_name and block.get("name") != tool_name:
                    continue
                arguments = block.get("input", block.get("arguments"))
                return json.loads(arguments) if isinstance(arguments, str) else arguments
    return None


def _extract_chat_text(resp):
    """把 /v1/chat/completions 的两种响应形态都归一化成 assistant 文本字符串。

    形态A（纯文本，OpenAI 兼容，顶层）：{"choices":[{"message":{"content":"<str>"}}]}
    形态B（多模态 doubao，envelope）：
      {"code":200,"data":{"message":{"content":[{"type":"output_text","text":"<str>"}]}}}
    见 basicrouter.ai/docs。找不到已知结构时回退返回 data 原样，供调用方兜底。
    """
    # 形态A：顶层 choices
    if isinstance(resp, dict) and "choices" in resp:
        message = resp["choices"][0]["message"]
        content = message.get("content")
        if isinstance(content, list):
            return "".join(str(item.get("text") or "") for item in content
                           if isinstance(item, dict))
        return content
    data = _unwrap(resp)  # 解 {code,message,data}；业务错误在此抛 BRError
    if isinstance(data, dict):
        if "choices" in data:
            return data["choices"][0]["message"]["content"]
        # 形态B：data.message.content 可能是 str 或 [{type,text}]
        msg = data.get("message")
        if isinstance(msg, dict):
            c = msg.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                parts = [seg.get("text", "") for seg in c
                         if isinstance(seg, dict) and seg.get("text")]
                if parts:
                    return "".join(parts)
        # 少数模型直接把文本放在 data.content / data.text
        if isinstance(data.get("content"), str):
            return data["content"]
        if isinstance(data.get("text"), str):
            return data["text"]
        if isinstance(data.get("content"), list):
            parts = [item.get("text", "") for item in data["content"]
                     if isinstance(item, dict) and item.get("type") in
                     {"text", "output_text"}]
            if parts:
                return "".join(parts)
    return data


def chat_stream(api_key, messages, model="qwen3.6-plus", timeout=300, **kw):
    """流式 chat completion（SSE），把增量 delta 拼成完整文本返回。

    为什么要流式：重推理视觉模型（如 kimi-k3）非流式一次响应要数分钟，网关会在
    首字节前把长连接断掉（"Remote end closed connection"，与 basic-router 524/499 同源）。
    stream:true 让网关持续吐 `data: {...}` 分片保活（文档给流式 300s 窗口 vs 非流式更短）。
    解析兼容两种分片：OpenAI 风格 choices[].delta.content，以及 data.message.content 增量。
    读取过程中 RemoteDisconnected/超时会重试整次请求（指数退避）。
    """
    url = api_endpoint_setup.base_url("text") + "/v1/chat/completions"
    body = {"model": model, "messages": messages, "stream": True}
    body.update(kw)
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    attempt = 0
    while True:
        chunks = []
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    piece = _extract_stream_delta(obj)
                    if piece:
                        chunks.append(piece)
            text = "".join(chunks)
            if text:
                return text
            # 空流：可能网关未真正支持该模型流式，抛错让上层降级到非流式
            raise BRError("streaming returned empty text")
        except (urllib.error.URLError, ConnectionError, EOFError) as e:
            # RemoteDisconnected 是 http.client 抛的 ConnectionError 子类，非流式路径漏接过它
            if attempt < MAX_RETRIES:
                attempt += 1
                time.sleep(_backoff_seconds(attempt))
                continue
            raise BRError("stream network error: %s" % e)


def _extract_stream_delta(obj):
    """从单个 SSE 分片对象里抠出增量文本，兼容多种形态。"""
    if not isinstance(obj, dict):
        return ""
    # OpenAI 风格：choices[].delta.content / message.content
    ch = obj.get("choices")
    if isinstance(ch, list) and ch:
        c0 = ch[0]
        if isinstance(c0, dict):
            delta = c0.get("delta") or c0.get("message") or {}
            if isinstance(delta, dict):
                cont = delta.get("content")
                if isinstance(cont, str):
                    return cont
                if isinstance(cont, list):
                    return "".join(seg.get("text", "") for seg in cont
                                   if isinstance(seg, dict))
    # envelope 风格：data.message.content 增量
    d = obj.get("data")
    if isinstance(d, dict):
        msg = d.get("message")
        if isinstance(msg, dict):
            cont = msg.get("content")
            if isinstance(cont, str):
                return cont
            if isinstance(cont, list):
                return "".join(seg.get("text", "") for seg in cont
                               if isinstance(seg, dict) and seg.get("text"))
    return ""


def create_image(api_key, text, model="seedream-5.0", count=1,
                 resolution="2k", ratio="1:1", image_urls=None, request_id=None,
                 timeout=180):
    """POST /ai/createImage -> list of image URLs."""
    if API_MODE != "legacy":
        task_id = create_image_generation(
            api_key, text, model=model, image_urls=image_urls,
            count=count, resolution=resolution, ratio=ratio,
            request_id=request_id)
        return wait_image_generation(api_key, task_id)
    body = {"model": model, "text": text, "count": count,
            "resolution": resolution, "ratio": ratio,
            "imageUrls": image_urls or []}
    request_id = request_id or uuid.uuid4().hex
    data = _unwrap(_request("POST", "/ai/createImage", api_key=api_key, body=body,
                             timeout=timeout, idempotency_key=request_id))
    return data.get("imageUrls", []) if isinstance(data, dict) else data


def create_image_generation(api_key, text, model="seedream-5.0", image_urls=None,
                             count=1, resolution=None, ratio=None, callback_url=None,
                             request_id=None):
    """POST /v1/image-generations (ASYNC) -> taskId.

    区别于同步 create_image()（POST /ai/createImage，立即返回 imageUrls）：这是
    v1 异步版，立即返回 taskId，需轮询 GET /v1/image-generations/{taskId} 或用
    callback_url webhook 拿结果。用于「商品图/网页截图/视频模板帧 + 用户需求描述」
    生成标准化素材的场景（见 asset_prep.py standardize）。

    imageUrls 是参考图（图生图）；本地文件先经 to_image_ref() 转 base64 data URL
    或托管 URL 再传入。resolution/ratio 的可选值应先查 GET /v1/image-models 核对
    （list_image_models()），仅接受该模型规格里公布的值。
    """
    body = {"model": model, "text": text}
    if image_urls:
        body["imageUrls"] = image_urls
    if count is not None:
        body["count"] = count
    if resolution:
        body["resolution"] = resolution
    if ratio:
        body["ratio"] = ratio
    if callback_url:
        body["callbackUrl"] = callback_url
    request_id = request_id or uuid.uuid4().hex
    data = _unwrap(_request("POST", "/v1/image-generations", api_key=api_key,
                            body=body, timeout=60, idempotency_key=request_id))
    if not isinstance(data, dict) or not data.get("taskId"):
        raise BRError("image-generations returned no taskId: %s" % json.dumps(data)[:300])
    return data["taskId"]


def get_image_generation(api_key, task_id):
    """GET /v1/image-generations/{taskId} -> {taskId, status, errorMessage, images, text}.

    status: pending / success / failed. images 是图片 URL 数组的 JSON 字符串
    （如 '["https://.../1.png"]'），成功前为 null。
    """
    return _unwrap(_request("GET", "/v1/image-generations/%s" % task_id,
                            api_key=api_key, timeout=30))


def wait_image_generation(api_key, task_id, interval=5, max_wait=900, on_tick=None):
    """Poll /v1/image-generations/{taskId} until success/failed. Returns list of image URLs.

    images 字段是 JSON 字符串数组，这里统一解析成 list[str] 再返回，调用方无需
    自己 json.loads。
    """
    waited = 0
    while waited <= max_wait:
        info = get_image_generation(api_key, task_id)
        status = (info.get("status") or "").lower()
        if on_tick:
            on_tick(status, waited)
        if status == "success":
            raw = info.get("images")
            if isinstance(raw, str):
                try:
                    urls = json.loads(raw)
                except json.JSONDecodeError:
                    urls = []
            elif isinstance(raw, list):
                urls = raw
            else:
                urls = []
            if urls:
                return urls
            raise BRError("image task succeeded but no images: %s" % json.dumps(info)[:300])
        if status == "failed":
            raise BRError("image task failed: %s" % (info.get("errorMessage") or info))
        time.sleep(interval)
        waited += interval
    raise BRError("timeout after %ss (task %s still %s)" % (max_wait, task_id, status))


def list_image_models(api_key=None):
    """GET /v1/image-models -> list of model dicts (id, displayName, maxCount, fileMax,

    resolutions[], ratios[]). 无需认证；调用 /v1/image-generations 前先查这个，
    只接受该模型规格里公布的 resolution/ratio 值。
    """
    data = _unwrap(_request("GET", "/v1/image-models", api_key=api_key, timeout=30))
    if isinstance(data, list):
        return data
    return data.get("data", []) if isinstance(data, dict) else []


def _video_catalog_route_index(records):
    """Index every advertised video route without collapsing shared IDs."""
    routes = []
    seen = set()
    for item in records or []:
        if not isinstance(item, dict):
            continue
        fingerprint = hashlib.sha256(json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            default=str).encode("utf-8")).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        names = []
        for key in ("modelName", "name", "displayName", "alias", "aliases",
                    "id", "modelId"):
            values = (_catalog_alias_values(item.get(key))
                      if key in ("alias", "aliases") else
                      [str(item.get(key)).strip()] if item.get(key) else [])
            names.extend(value for value in values
                         if value and value not in names)
        identities = [str(item.get(key)).strip()
                      for key in ("id", "modelId") if item.get(key)]
        routes.append({
            "item": item, "names": names, "identities": identities,
            "provider_names": [name for name in names
                               if name not in identities],
        })
    aliases = {}
    for index, route in enumerate(routes):
        for name in route["names"]:
            aliases.setdefault(name.lower(), set()).add(index)
    return routes, aliases


def _legacy_video_model_name(model, *, catalog_records=None,
                             catalog_aliases=None):
    """Translate catalog IDs to the provider-facing video submission name.

    The live catalog exposes canonical model IDs while the video generation
    endpoint accepts provider-facing model names (for example ``seedance-2.0``
    instead of ``dreamina-seedance-2-0-260128``). Prefer the live catalog's
    modelName/alias and retain only a narrow offline fallback for known gateway
    pairs.
    """
    if not model:
        return model
    seedance_names = _seedance_submission_names(model)
    routes = catalog_records
    aliases = catalog_aliases
    if routes is None or aliases is None:
        try:
            routes, aliases = _video_catalog_route_index(
                list_video_models(None))
        except (BRError, OSError, ValueError, TypeError, KeyError):
            routes, aliases = [], {}
    requested = str(model).strip()
    matches = aliases.get(requested.lower(), set())
    if len(matches) > 1:
        raise BRError("AMBIGUOUS_MODEL_ALIAS: %s" % requested)
    if len(matches) == 1:
        route = routes[next(iter(matches))]
        unique_provider_names = [
            name for name in route["provider_names"]
            if aliases.get(name.lower()) == matches]
        if requested in route["identities"] and unique_provider_names:
            requested = unique_provider_names[0]
        mapped = _seedance_submission_names(requested)
        return mapped[0] if mapped else requested
    for seedance_name in seedance_names:
        seedance_matches = aliases.get(seedance_name.lower(), set())
        if len(seedance_matches) == 1:
            return seedance_name
    return {
        # These are offline-only aliases. Live catalog names always win and
        # unknown names must not be rewritten into a guessed model ID.
        "seedance-2.0": "seedance-2.0-VS-white",
        "seedance-2.0-white": "seedance-2.0-VS-white",
        "dreamina-seedance-2-0-260128": "seedance-2.0-VS-white",
        "dreamina-seedance-2-0-fast-260128": "seedance-2.0-fast",
        "wan2.7-i2v": "wan2.7-i2v",
    }.get(str(model).strip(), model)


def _legacy_video_model_candidates(model):
    """Return provider-advertised names to try for legacy createVideo.

    Catalog identity and legacy submission name are not the same contract.
    Retry is restricted to model-not-found responses, so no paid generation is
    duplicated for parameter, policy, credit, or network failures.
    """
    requested = str(model or "").strip()
    seedance_preferred = _seedance_submission_names(requested)
    candidates = []
    try:
        routes, alias_index = _video_catalog_route_index(
            list_video_models(None))
        requested_matches = alias_index.get(requested.lower(), set())
        if len(requested_matches) > 1:
            raise BRError("AMBIGUOUS_MODEL_ALIAS: %s" % requested)
        for route_index, route in enumerate(routes):
            item = route["item"]
            # Legacy createVideo expects the provider-facing modelName/alias,
            # while modelId is often an internal catalog identifier. Keep the
            # former ahead of the latter when both are present.
            values = route["names"]
            identity_values = route["identities"]
            provider_values = route["provider_names"]
            if route_index not in requested_matches:
                continue
            unique_values = [
                value for value in values
                if alias_index.get(value.lower()) == {route_index}]
            unique_provider_values = [
                value for value in provider_values if value in unique_values]
            # If the caller supplied an internal catalog ID, submit the
            # provider-facing name first. The internal ID is only a fallback
            # after a specific model-not-found response, and only when that ID
            # is unique across all provider routes.
            if requested in identity_values and unique_provider_values:
                ordered = unique_provider_values + (
                    [requested] if requested in unique_values else [])
            else:
                ordered = ([requested] if requested in unique_values else []) + [
                    value for value in unique_values if value != requested]
            candidates.extend(value for value in ordered if value and value not in candidates)
    except BRError as exc:
        if str(exc).startswith("AMBIGUOUS_MODEL_ALIAS"):
            raise
        routes, alias_index = [], {}
    except (OSError, ValueError, TypeError, KeyError):
        routes, alias_index = [], {}
    if not candidates and seedance_preferred:
        candidates.extend(seedance_preferred)
    requested_is_ambiguous = len(alias_index.get(requested.lower(), set())) > 1
    if requested and requested not in candidates and not requested_is_ambiguous:
        candidates.append(requested)
    fallback = _legacy_video_model_name(
        requested, catalog_records=routes, catalog_aliases=alias_index)
    if fallback not in candidates:
        candidates.append(fallback)
    # Keep the Kling legacy pair available in both directions. Live catalogs
    # have alternated between ``kling-v3-omni`` and ``kling-v3-omni-video``
    # as the accepted submission name, so retry the sibling only after a
    # model-not-found response. This stays fail-closed for other error types.
    if requested == "kling-v3-omni-video":
        sibling = "kling-v3-omni"
    elif requested == "kling-v3-omni":
        sibling = "kling-v3-omni-video"
    else:
        sibling = None
    if sibling and sibling not in candidates:
        candidates.append(sibling)
    return candidates


def _catalog_alias_values(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = [value]
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    return [str(item).strip() for item in value if item]


def _seedance_submission_names(value):
    text = str(value or "").strip().lower()
    if text in {
        "seedance-2.0",
        "seedance-2.0-white",
        "seedance-2.0-vs-white",
        "dreamina-seedance-2-0-260128",
    }:
        return ["seedance-2.0-VS-white"]
    return []


def create_video(api_key, text, model="seedance-2.0-VS-white", video_type=1,
                 urls=None, resolution="1080p", ratio="9:16", duration=5,
                 negative_prompt=None, seed=None, extra=None, extend_video_url=None,
                 request_id=None):
    """POST /v1/video-generations (async) -> taskId.

    高级参数（实测网关接受，提升画质/一致性）：
      resolution="1080p"     默认拉到 1080p（实测真出 1920x1080，画质翻倍，观感更高级）
      negative_prompt=...    负向约束（仅旧兼容入口使用；当前 v1 文档未声明该字段）
      seed=<int>             固定随机种子（仅旧兼容入口使用；当前 v1 文档未声明该字段）
      extra={...}            其它想透传的字段（前向兼容新参数）
    未传的高级参数不进 body，保持与旧行为一致。
    """
    request_id = request_id or uuid.uuid4().hex
    candidates = _legacy_video_model_candidates(model)
    last_error = None
    for idx, submit_model in enumerate(candidates):
        body = {"model": submit_model, "text": text, "videoType": video_type,
                "imageUrls": urls or [], "resolution": resolution,
                "ratio": ratio, "duration": duration}
        if extend_video_url:
            body["videoUrls"] = [extend_video_url]
        if extra:
            body.update(extra)
        submit_request_id = request_id if idx == 0 else "%s:%d" % (request_id, idx)
        try:
            data = _unwrap(_request(
                "POST", "/v1/video-generations", api_key=api_key, body=body,
                timeout=60, idempotency_key=submit_request_id))
            if not isinstance(data, dict) or not data.get("taskId"):
                raise BRError("createVideo returned no taskId: %s" % json.dumps(data)[:300])
            return data["taskId"]
        except BRError as error:
            if not is_video_model_not_found(error) or idx >= len(candidates) - 1:
                raise
            last_error = error
    if last_error:
        raise last_error
    raise BRError("createVideo returned no taskId: %s" % model)


def get_video(api_key, task_id):
    """GET /v1/video-generations/{taskId} documented video task status endpoint."""
    data = _unwrap(_request("GET", "/v1/video-generations/%s" %
                           urllib.parse.quote(str(task_id), safe=""),
                           api_key=api_key, timeout=30))
    return data.get("data", data) if isinstance(data, dict) else data


def wait_video(api_key, task_id, interval=8, max_wait=3600, on_tick=None):
    """Poll until succeeded/failed. Returns videoUrl on success, raises on failure/timeout."""
    waited = 0
    current_interval = max(1, interval)
    while waited <= max_wait:
        info = get_video(api_key, task_id)
        status = (info.get("status") or "").lower()
        if on_tick:
            on_tick(status, waited)
        if status in ("succeeded", "succeed", "success", "completed"):
            if info.get("videoUrl"):
                return info["videoUrl"]
            raise BRError("succeeded but no videoUrl: %s" % json.dumps(info)[:300])
        if status in ("failed", "error"):
            raise video_task_error(info)
        sleep_for = min(current_interval, max_wait - waited)
        if sleep_for <= 0:
            break
        time.sleep(sleep_for)
        waited += sleep_for
        current_interval = min(30, max(current_interval, 15))
    raise BRError("timeout after %ss (task %s still %s)" % (max_wait, task_id, status))


def wait_video_full(api_key, task_id, interval=8, max_wait=3600, on_tick=None):
    """Like wait_video but returns the full result dict {videoUrl, lastFrameUrl, ...}."""
    waited = 0
    current_interval = max(1, interval)
    while waited <= max_wait:
        info = get_video(api_key, task_id)
        status = (info.get("status") or "").lower()
        if on_tick:
            on_tick(status, waited)
        if status in ("succeeded", "succeed", "success", "completed"):
            if info.get("videoUrl"):
                return info  # caller gets lastFrameUrl too
            raise BRError("succeeded but no videoUrl: %s" % json.dumps(info)[:300])
        if status in ("failed", "error"):
            raise video_task_error(info)
        sleep_for = min(current_interval, max_wait - waited)
        if sleep_for <= 0:
            break
        time.sleep(sleep_for)
        waited += sleep_for
        current_interval = min(30, max(current_interval, 15))
    raise BRError("timeout after %ss (task %s still %s)" % (max_wait, task_id, status))


_TRANSPARENT_PROXY_FAKE_IP = ipaddress.ip_network("198.18.0.0/15")


def _provider_download_address_allowed(address, allow_nonpublic_peer=False):
    if address.is_global:
        return True
    # macOS transparent proxies commonly map public domains into RFC 2544's
    # benchmarking range.  Only provider-completed downloads explicitly pass
    # allow_nonpublic_peer=True; keep loopback, link-local and RFC1918 blocked.
    return bool(
        allow_nonpublic_peer and address.version == 4 and
        address in _TRANSPARENT_PROXY_FAKE_IP)


def _validate_download_url(url, allow_nonpublic_peer=False):
    if not isinstance(url, str) or any(ord(char) < 32 for char in url):
        raise BRError("DOWNLOAD_URL_BLOCKED: 下载地址格式无效")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise BRError("DOWNLOAD_URL_BLOCKED: 下载地址格式无效") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or port not in (None, 443)):
        raise BRError("DOWNLOAD_URL_BLOCKED: 只允许 HTTPS 下载地址")
    hostname = parsed.hostname.rstrip(".")
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        address = None
    addresses = [address] if address else []
    if not address:
        try:
            addresses = [ipaddress.ip_address(item[4][0].split("%", 1)[0])
                         for item in socket.getaddrinfo(hostname, port or 443,
                                                        type=socket.SOCK_STREAM)]
        except (socket.gaierror, ValueError) as exc:
            raise BRError("DOWNLOAD_URL_BLOCKED: DNS 解析失败") from exc
    if not addresses or any(
            not _provider_download_address_allowed(
                item, allow_nonpublic_peer=allow_nonpublic_peer)
            for item in addresses):
        raise BRError("DOWNLOAD_URL_BLOCKED: 不允许私网、回环或保留地址")
    return url


def _normalize_ip(value):
    """Normalize IPv4, IPv6 zone ids and IPv4-mapped IPv6 addresses."""
    value = str(value).strip().strip("[]").split("%", 1)[0]
    address = ipaddress.ip_address(value)
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped or address


def _configured_proxy_peers():
    """Return explicitly configured proxy peers allowed for HTTPS downloads."""
    peers = set()
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                "ALL_PROXY", "all_proxy"):
        value = os.environ.get(key)
        if not value:
            continue
        try:
            parsed = urllib.parse.urlsplit(value)
            if parsed.hostname:
                host = parsed.hostname.strip("[]")
                port = parsed.port or 443
                for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
                    peers.add(_normalize_ip(item[4][0]))
        except (ValueError, socket.gaierror):
            continue
    return peers


def _url_resolves_to_transparent_proxy_fake_ip(url):
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
        if parsed.scheme != "https" or not parsed.hostname:
            return False
        addresses = [
            _normalize_ip(item[4][0])
            for item in socket.getaddrinfo(
                parsed.hostname, parsed.port or 443,
                type=socket.SOCK_STREAM)]
    except (ValueError, socket.gaierror):
        return False
    return bool(addresses) and all(
        address.version == 4 and address in _TRANSPARENT_PROXY_FAKE_IP
        for address in addresses)


def _peer_is_acceptable(peer, allow_nonpublic=False, trusted_url=None):
    normalized = _normalize_ip(peer)
    if _provider_download_address_allowed(
            normalized, allow_nonpublic_peer=allow_nonpublic):
        return True
    # In transparent-proxy mode DNS advertises an RFC 2544 fake IP while the
    # local interception socket is loopback.  Admit that exact paired shape
    # only for a provider-completed HTTPS URL; arbitrary localhost/RFC1918
    # destinations remain blocked by URL validation and this peer check.
    if (allow_nonpublic and normalized.is_loopback and trusted_url and
            _url_resolves_to_transparent_proxy_fake_ip(trusted_url)):
        return True
    # A corporate/local HTTPS proxy is an explicit operator choice. Allowing
    # its socket peer does not make arbitrary direct URLs trusted: the URL is
    # still HTTPS-only, DNS-validated, and the proxy is resolved from env.
    return normalized in _configured_proxy_peers()


class _ValidatedDownloadRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every redirect before urllib opens the next connection."""

    def __init__(self, allow_nonpublic_peer=False):
        super().__init__()
        self.allow_nonpublic_peer = allow_nonpublic_peer

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Validate the socket that served the redirect before following it.
        # URL/DNS validation alone is insufficient against intermediate-hop
        # DNS rebinding or a public endpoint redirecting from a private peer.
        try:
            peer = _download_peer(fp)
            if not _peer_is_acceptable(
                    peer, self.allow_nonpublic_peer,
                    trusted_url=req.full_url):
                raise BRError("DOWNLOAD_PEER_BLOCKED: redirect peer is not public")
        except AttributeError:
            raise BRError("DOWNLOAD_PEER_UNVERIFIED: redirect peer unavailable")
        _validate_download_url(
            newurl, allow_nonpublic_peer=self.allow_nonpublic_peer)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_download(request, timeout, allow_nonpublic_peer=False):
    handler = _ValidatedDownloadRedirectHandler(allow_nonpublic_peer=allow_nonpublic_peer)
    return urllib.request.build_opener(handler).open(
        request, timeout=timeout)


def _download_peer(response):
    candidates = [
        getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
        getattr(getattr(getattr(getattr(response, "fp", None), "raw", None),
                        "_connection", None), "sock", None),
        getattr(response, "_sock", None),
    ]
    for candidate in candidates:
        if candidate and hasattr(candidate, "getpeername"):
            try:
                return candidate.getpeername()[0]
            except OSError:
                continue
    raise BRError("DOWNLOAD_PEER_UNVERIFIED: 无法验证实际连接地址")


def download(url, dest_path, timeout=180, max_retries=MAX_RETRIES, on_retry=None,
             max_bytes=2 * 1024 * 1024 * 1024, allow_nonpublic_peer=False):
    """Atomically download a URL, preserving any existing destination on failure."""
    allow_nonpublic_peer = bool(
        allow_nonpublic_peer or
        os.environ.get("BASICROUTER_ALLOW_NONPUBLIC_DOWNLOAD_PEER") == "1")
    _validate_download_url(
        url, allow_nonpublic_peer=allow_nonpublic_peer)
    dest_path = os.path.abspath(dest_path)
    dest_dir = os.path.dirname(dest_path)
    if not os.path.isdir(dest_dir) or os.path.islink(dest_dir) or os.path.islink(dest_path):
        raise BRError("DOWNLOAD_DESTINATION_BLOCKED: 目标或父目录不能是符号链接")
    attempt = 0
    while True:
        temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix=".%s." % os.path.basename(dest_path),
                                             suffix=".tmp", dir=dest_dir)
            request = urllib.request.Request(url, headers={"User-Agent": "BasicRouter-Media/1"})
            opener = (_open_download(request, timeout, allow_nonpublic_peer=allow_nonpublic_peer)
                      if allow_nonpublic_peer else _open_download(request, timeout))
            with os.fdopen(fd, "wb") as f, opener as r:
                final_url = r.geturl()
                _validate_download_url(
                    final_url, allow_nonpublic_peer=allow_nonpublic_peer)
                if not _peer_is_acceptable(
                        _download_peer(r), allow_nonpublic_peer,
                        trusted_url=final_url):
                    raise BRError("DOWNLOAD_PEER_BLOCKED: 实际连接到了非公网地址")
                expected = None
                headers = getattr(r, "headers", None)
                if headers is not None:
                    raw_length = headers.get("Content-Length")
                    if raw_length:
                        try:
                            expected = int(raw_length)
                        except (TypeError, ValueError):
                            expected = None
                if expected is not None and expected > max_bytes:
                    raise BRError("DOWNLOAD_TOO_LARGE: %d > %d" % (expected, max_bytes))
                received = 0
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
                    if received > max_bytes:
                        raise BRError("DOWNLOAD_TOO_LARGE: 超过 %d bytes" % max_bytes)
                if expected is not None and received != expected:
                    raise EOFError(
                        "truncated download: expected %d bytes, received %d" %
                        (expected, received))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, dest_path)
            temp_path = None
            return dest_path
        except urllib.error.HTTPError as e:
            if attempt < max_retries and e.code in RETRY_STATUS:
                attempt += 1
                wait = _backoff_seconds(attempt)
                if on_retry:
                    on_retry(attempt, wait, "HTTP %s" % e.code)
                time.sleep(wait)
                continue
            raise BRError("download HTTP %s" % e.code, http_status=e.code)
        except (urllib.error.URLError, ConnectionError, EOFError, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            if attempt < max_retries:
                attempt += 1
                wait = _backoff_seconds(attempt)
                if on_retry:
                    on_retry(attempt, wait, "network: %s" % reason)
                time.sleep(wait)
                continue
            raise BRError("network error: %s" % reason)
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass


def _to_data_url(path):
    from image_utils import image_mime_type
    mime = image_mime_type(path)
    if not mime:
        raise BRError("invalid image file: %s (请上传有效的 PNG/JPEG/WebP 图片)" % path)
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return "data:%s;base64,%s" % (mime, b64)


# ── host_image 进程级缓存 ──────────────────────────────────────────────────────
# 同一本地文件在一次 batch/chain 里只上传/重绘一次。
# key = (abspath, file_sha256, api_key_sha256) → hosted_url
# 原理：host_image 走 img2img 重绘，相同图片多次调用会产出不同结果（浪费 Credit
# 且破坏一致性）。confirmed 素材应始终对应同一张托管图，不能因 N 段同时引用而
# 被重绘 N 次。进程级缓存（非文件缓存）保持轻量，重启后自动清空。
_HOST_IMAGE_CACHE: dict = {}


def host_image(api_key, path, timeout=600):
    """Upload a local image and return a hosted https URL.

    Implementation: send the local file as a base64 data URL to the documented
    async image-generation endpoint, then retrieve the generated image URL. The
    platform returns a hosted URL we can reuse for createVideo (whose endpoint
    rejects very large data-URL bodies). This is the 'no external image host
    needed' path — the platform itself hosts it.

    进程级缓存：同一路径的相同内容和凭据只上传一次，防止 batch/chain 里同一张
    confirmed 参考图被重绘 N 次（浪费 Credit + 破坏一致性）。
    """
    abs_path = os.path.abspath(path)
    with open(abs_path, "rb") as f:
        image_bytes = f.read()
    content_sha = hashlib.sha256(image_bytes).hexdigest()
    key_sha = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    cache_key = (abs_path, content_sha, key_sha)
    if cache_key in _HOST_IMAGE_CACHE:
        return _HOST_IMAGE_CACHE[cache_key]
    from image_utils import image_mime_type
    mime = image_mime_type(abs_path)
    if not mime:
        raise BRError("invalid image file: %s (请上传有效的 PNG/JPEG/WebP 图片)" % abs_path)
    data_url = "data:%s;base64,%s" % (mime, base64.b64encode(image_bytes).decode())
    request_id = "host-" + hashlib.sha256(
        (content_sha + key_sha).encode("ascii")).hexdigest()
    task_id = create_image_generation(
        api_key, "keep the image unchanged, clean output",
        model="kling-v3-omni-image", count=1,
        resolution="2k", ratio="9:16", image_urls=[data_url],
        request_id=request_id)
    try:
        urls = wait_image_generation(api_key, task_id, interval=5, max_wait=timeout)
    except BRError as exc:
        if "timeout" not in str(exc).lower() and "超时" not in str(exc):
            raise
        # Retrieval timed out after task submission. Re-poll the same task ID
        # rather than submitting a second billable hosting request.
        urls = wait_image_generation(api_key, task_id, interval=5, max_wait=timeout)
    if not urls:
        raise BRError("host_image: platform returned no URL")
    result = urls[0]
    _HOST_IMAGE_CACHE[cache_key] = result
    return result


def to_image_ref(path_or_url, api_key=None, prefer_hosted=False, host_timeout=600):
    """Normalize an image reference for the API.

    - http(s)/data URL: passthrough.
    - local file + prefer_hosted: legacy pseudo-hosting via image generation.
      Do not use this for /v1/video-generations; video imageUrls must be the
      HTTP(S) URLs returned by the original image retrieve result.
    - local file otherwise (image use): inline base64 data URL.
    """
    if not path_or_url:
        return path_or_url
    if path_or_url.startswith(("http://", "https://", "data:")):
        return path_or_url
    if not os.path.isfile(path_or_url):
        raise BRError("image not found: %s" % path_or_url)
    # Reject placeholders/corrupt uploads before spending an image-generation
    # request. The remote provider otherwise reports a vague HTTP 400 later.
    try:
        from image_utils import image_type
        detected_type = image_type(path_or_url)
    except (ImportError, OSError) as exc:
        raise BRError("image unreadable: %s (%s)" % (path_or_url, exc))
    if not detected_type:
        raise BRError("invalid image file: %s (请上传有效的 PNG/JPEG/WebP 图片)" % path_or_url)
    if prefer_hosted:
        if not api_key:
            raise BRError("to_image_ref(prefer_hosted) needs api_key")
        return host_image(api_key, path_or_url, timeout=host_timeout)
    return _to_data_url(path_or_url)
