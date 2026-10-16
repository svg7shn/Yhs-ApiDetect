"""Bounded, original channel probes. Heuristics never authenticate provenance."""
from __future__ import annotations

import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .channel_security import ChannelError, normalize_url, redact, request_json

FAMILY_LABELS = {"claude": "Claude", "gpt": "GPT", "gemini": "Gemini"}
SOURCES = [
    {"id": f"{family}_{suffix}", "label": label, "family": family}
    for family, rows in [
        ("claude", [("official", "Anthropic 官方"), ("ccmax", "Claude Code + Max 订阅"),
                    ("bedrock", "Amazon Bedrock"), ("aws_partner", "AWS 合作伙伴"),
                    ("q_kiro", "Amazon Q / Kiro"), ("vertex", "Vertex AI（Claude）"), ("azure", "Azure（Claude）")]),
        ("gpt", [("official", "OpenAI 官方"), ("azure", "Azure OpenAI")]),
        ("gemini", [("official", "Google AI Studio"), ("vertex", "Vertex AI（Gemini）")]),
    ] for suffix, label in rows
]
SOURCE_MAP = {s["id"]: s for s in SOURCES}
MAX_PAGES, MAX_MODELS = 5, 1000
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
# 参照 new-api 的 protocol.go 合法取值集合，仅做字段形态判定，不证明来源。
CLAUDE_STOP_REASONS = {"end_turn", "max_tokens", "tool_use", "stop_sequence", "pause_turn", "refusal",
                       "model_context_window_exceeded"}
# 判定阈值与最小有效检查数。有效项不足时不给结论，回落 inconclusive。
AUTHENTIC_THRESHOLD = 75.0
SUSPECT_THRESHOLD = 50.0
MIN_SCORED_SIGNALS = 3
# 订阅/合作伙伴来源无法通过本 API 证明，永不判为 authentic。
UNPROVABLE_SOURCES = {"claude_ccmax", "claude_q_kiro", "claude_aws_partner"}


def family_of(model):
    model = model.lower()
    if "claude" in model:
        return "claude"
    if "gemini" in model:
        return "gemini"
    if any(x in model for x in ("gpt", "openai")) or re.search(r"(?:^|/)o[134](?:-|$)", model):
        return "gpt"
    return None


def endpoint(base, suffix, family="gpt"):
    path = urlsplit(base).path.rstrip("/")
    if not path:
        base += "/v1beta" if family == "gemini" else "/v1"
    elif family == "gemini" and path.endswith("/v1"):
        base = base[:-3] + "/v1beta"
    return base + "/" + suffix


async def list_models(base, key):
    base = normalize_url(base)
    urls = list(dict.fromkeys([endpoint(base, "models"), endpoint(base, "models", "gemini")]))
    selected = None
    payload = None
    for url in urls:
        try:
            payload = await request_json("GET", url, key, timeout=10)
            selected = url
            break
        except ChannelError as exc:
            if exc.code != "not_found":
                raise
    if selected is None:
        raise ChannelError("not_found", "模型列表端点不存在（404），未验证凭据", 404)
    found, seen_pages, params = {}, set(), None
    for page in range(MAX_PAGES):
        if page:
            payload = await request_json("GET", selected, key, params=params, timeout=10)
        rows = payload if isinstance(payload, list) else payload.get("data", payload.get("models")) if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ChannelError("invalid_response", "模型列表结构无效", 502)
        for row in rows:
            model = row if isinstance(row, str) else row.get("id", row.get("name")) if isinstance(row, dict) else None
            if isinstance(model, str) and MODEL_RE.fullmatch(model) and key not in model:
                family = family_of(model)
                if family:
                    found[model] = {"id": model, "family": family}
            if len(found) > MAX_MODELS:
                raise ChannelError("model_limit", "模型列表超过数量限制", 502)
        params = None
        if isinstance(payload, dict):
            if payload.get("nextPageToken"):
                params = {"pageToken": payload["nextPageToken"]}
            elif payload.get("has_more"):
                params = {"after_id": payload.get("last_id") or (rows[-1].get("id") if rows and isinstance(rows[-1], dict) else None)}
            elif payload.get("next") or payload.get("next_url"):
                raise ChannelError("pagination_unsupported", "不接受上游分页跳转地址", 502)
        if not params:
            return {"ok": True, "base_url": redact(base, key), "models": list(found.values()), "endpoint": redact(selected, key)}
        cursor = next(iter(params.values()))
        if not isinstance(cursor, str) or not cursor or len(cursor) > 1024 or cursor in seen_pages or key in cursor:
            raise ChannelError("invalid_pagination", "上游分页游标无效或循环", 502)
        seen_pages.add(cursor)
    raise ChannelError("page_limit", "模型分页超过上限，未返回不完整列表", 502)


def challenge():
    a, b, c = [secrets.randbelow(800) + 100 for _ in range(3)]
    values = [secrets.randbelow(100) for _ in range(12)]
    prompt = (f"Compute: 1) {a} * {b}; 2) ({a} + {c}) * 2 - {b}; "
              f"3) count numbers divisible by 3 in {values}. "
              "Reply exactly ANSWER1: integer, ANSWER2: integer, ANSWER3: integer on separate lines. "
              "Then add IDENTITY: your model family and creator. Do not guess your deployment provider.")
    return prompt, [a*b, (a+c)*2-b, sum(v % 3 == 0 for v in values)]


def probe_body(model, family, prompt):
    if family == "gemini":
        return {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 512}}
    return {"model": model, "messages": [{"role": "user", "content": prompt}],
            ("max_tokens" if family == "claude" else "max_completion_tokens"): 512}


def evidence(name, status, detail, category="protocol", weight=1.0, signal_id=None):
    """一条检测证据。weight=0 表示信息性证据，展示但仍不计入加权分。"""
    return {"name": name, "status": status, "detail": detail,
            "id": signal_id or name, "category": category, "weight": weight,
            "pass": status == "pass"}


def weighted_score(scored):
    """加权通过率：仅统计有判定意义（pass/fail）且 weight>0 的证据。"""
    total = sum(float(e.get("weight") or 0) for e in scored)
    if not total:
        return None
    passed = sum(float(e.get("weight") or 0) for e in scored if e["status"] == "pass")
    return round(100 * passed / total, 1)


def inspect_payload(payload, family, answers, wire_family=None, source=None, stats=None):
    ev = []
    if not isinstance(payload, dict):
        return [evidence("协议结构", "fail", "响应不是对象")]
    wire_family = wire_family or family
    try:
        if wire_family == "claude":
            if payload.get("stop_reason") in ("max_tokens", "refusal"):
                return [evidence("输出完整性", "unknown", "输出被截断或拒绝，不能评估能力或来源")]
            text = "\n".join(x.get("text", "") for x in payload["content"] if x.get("type") == "text")
            shape = payload.get("type") == "message" and str(payload.get("id", "")).startswith("msg_")
            usage, required = payload.get("usage", {}), ("input_tokens", "output_tokens")
        elif wire_family == "gpt":
            choice = payload["choices"][0]
            if choice.get("finish_reason") in ("length", "content_filter", "refusal") or choice.get("message", {}).get("refusal"):
                return [evidence("输出完整性", "unknown", "输出被截断或拒绝，不能评估能力或来源")]
            text = choice["message"]["content"]
            shape = payload.get("object") == "chat.completion" and str(payload.get("id", "")).startswith("chatcmpl-")
            usage, required = payload.get("usage", {}), ("prompt_tokens", "completion_tokens", "total_tokens")
        else:
            feedback = payload.get("promptFeedback", {})
            if feedback.get("blockReason") or any(r.get("blocked") for r in feedback.get("safetyRatings", [])):
                return [evidence("输出完整性", "unknown", "提示被安全策略阻止，不能评估能力或来源")]
            candidate = payload["candidates"][0]
            if candidate.get("finishReason") in ("MAX_TOKENS", "SAFETY"):
                return [evidence("输出完整性", "unknown", "输出被截断或拒绝，不能评估能力或来源")]
            text = "\n".join(x.get("text", "") for x in candidate["content"]["parts"])
            shape = isinstance(payload.get("modelVersion"), str)
            usage, required = payload.get("usageMetadata", {}), ("promptTokenCount", "candidatesTokenCount", "totalTokenCount")
        if not isinstance(text, str):
            raise ValueError
        valid_usage = isinstance(usage, dict) and all(isinstance(usage.get(k), int) and not isinstance(usage[k], bool) and usage[k] >= 0 for k in required)
        if wire_family == "claude":
            mid = str(payload.get("id") or "")
            ev.append(evidence("消息 ID 前缀", "pass" if mid.startswith("msg_") else "fail",
                               f"消息 ID 应以 msg_ 前缀，实际：{mid[:48] or '（缺省）'}", "protocol", 1.5, "message_id_prefix"))
            ev.append(evidence("响应类型", "pass" if payload.get("type") == "message" else "fail",
                               "顶层 type 应为 message", "protocol", 1.0, "response_type"))
            stop = payload.get("stop_reason")
            if not isinstance(stop, str) or not stop:
                ev.append(evidence("停止原因", "unknown", "响应未含 stop_reason，本次不判定", "protocol", 0.0, "stop_reason_valid"))
            else:
                ev.append(evidence("停止原因", "pass" if stop in CLAUDE_STOP_REASONS else "fail",
                                   f"stop_reason 应属 Anthropic 合法取值，实际：{stop}", "protocol", 1.0, "stop_reason_valid"))
            ev.append(claude_source_signal(payload, source))
        elif wire_family == "gpt":
            mid = str(payload.get("id") or "")
            ev.append(evidence("响应 ID 前缀", "pass" if mid.startswith("chatcmpl-") else "fail",
                               f"响应 ID 应以 chatcmpl- 前缀，实际：{mid[:48] or '（缺省）'}", "protocol", 1.5, "chatcmpl_id_prefix"))
            ev.append(evidence("响应对象类型", "pass" if payload.get("object") == "chat.completion" else "fail",
                               "顶层 object 应为 chat.completion", "protocol", 1.0, "object_type"))
            finish = choice.get("finish_reason")
            if not isinstance(finish, str) or not finish:
                ev.append(evidence("完成原因", "unknown", "响应未含 finish_reason，本次不判定", "protocol", 0.0, "finish_reason_valid"))
            else:
                ev.append(evidence("完成原因", "pass" if finish == "stop" else "fail",
                                   f"finish_reason 应为 stop，实际：{finish}", "protocol", 0.8, "finish_reason_valid"))
        else:
            candidates = payload.get("candidates")
            ev.append(evidence("candidates 数组", "pass" if isinstance(candidates, list) and candidates else "fail",
                               "应存在非空 candidates 数组", "protocol", 1.5, "candidates_present"))
            mv = payload.get("modelVersion")
            if not isinstance(mv, str) or not mv:
                ev.append(evidence("模型版本字段", "unknown", "响应未含 modelVersion，本次不判定", "protocol", 0.0, "model_version_present"))
            else:
                ev.append(evidence("模型版本字段", "pass", f"modelVersion 在场：{mv[:48]}", "protocol", 0.8, "model_version_present"))
        ev.append(evidence("计量结构", "pass" if valid_usage else "fail",
                           "计量字段结构检查，不验证上游账单", "protocol", 1.5, "usage_schema"))
        results = []
        for i, answer in enumerate(answers, 1):
            m = re.search(rf"(?m)^ANSWER{i}:\s*(-?\d+)\s*$", text)
            results.append(m is not None and len(m[1]) < 20 and int(m[1]) == answer)
        correct, total = sum(results), len(results)
        cap_accuracy = round(correct / total, 4) if total else -1.0
        if stats is not None:
            stats["capability_accuracy"] = cap_accuracy
        if total and correct == total:
            ev.append(evidence("能力探针", "pass",
                               f"能力探针正确率 {correct}/{total}：题目由程序生成，仅证明本次回答符合预期，非型号或来源证明",
                               "capability", 1.0, "capability_probe"))
        elif total and correct == 0:
            ev.append(evidence("能力探针", "fail",
                               f"能力探针正确率 0/{total}：可能为同族降级或能力截断，须结合基线复核",
                               "capability", 2.0, "capability_probe"))
        else:
            ev.append(evidence("能力探针", "unknown",
                               f"能力探针正确率 {correct}/{total}：部分正确，不足以判定能力异常",
                               "capability", 0.0, "capability_probe"))
        identity = re.search(r"(?im)^IDENTITY:(.*)$", text)
        match = identity and any(x in identity[1].lower() for x in {"claude": ["claude", "anthropic"], "gpt": ["gpt", "openai"], "gemini": ["gemini", "google"]}[family])
        ev.append(evidence("自报身份", "pass" if match else "unknown", "自报包含声称模型族（弱信号，可诱导）" if match else "未获得可匹配身份；不据此认定伪造"))
        echo = payload.get("model", payload.get("modelVersion"))
        ev.append(evidence("回显模型", "pass" if isinstance(echo, str) and family_of(echo) == family else "unknown", "仅比较模型族，不采信回显为来源证明"))
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        ev.append(evidence("协议结构", "fail", "模型输出结构不符合所选有限协议"))
    # Inspect presence only: raw fields (including signatures and identity text) are never stored.
    ev.append(evidence("签名字段", "unknown", "仅观察签名字段；没有可信公钥或验证器，未执行密码学验签。" + ("观察到字段。" if has_field(payload, {"signature", "thoughtSignature"}) else "未观察到字段。")))
    ev.append(evidence("缓存字段", "unknown", "观察到缓存计量字段；未做缓存写入/命中因果验证，可被伪造。" if has_field(payload, {"cached_tokens", "cache_read_input_tokens", "cachedContentTokenCount"}) else "未观察到缓存字段；未命中不作为负面证据。"))
    return ev


def claude_source_signal(payload, source):
    """按响应消息 ID 前缀核对声称来源。只用响应里已有的字段，不新增上游请求。"""
    mid = str(payload.get("id") or "")
    bdrk = mid.startswith(("msg_bdrk_", "toolu_bdrk_"))
    vrtx = mid.startswith(("msg_vrtx_", "toolu_vrtx_"))
    detail = f"消息 ID 前缀（{mid[:32] or '（缺省）'}）不足以区分所选来源，本次不判"
    if not mid:
        return evidence("Claude 来源溯源", "unknown", "响应未含消息 ID，本次不判溯源", "residue", 0.0, "claude_source_provenance")
    if source == "claude_bedrock":
        if bdrk:
            return evidence("Claude 来源溯源", "pass", "消息 ID 为 Bedrock 前缀，符合 AWS Bedrock 来源", "residue", 1.5, "claude_source_provenance")
        if vrtx:
            return evidence("Claude 来源溯源", "fail", f"声称 AWS Bedrock，但消息 ID 为 Vertex 前缀（{mid[:32]}）", "residue", 4.0, "claude_source_provenance")
    elif source == "claude_vertex":
        if vrtx:
            return evidence("Claude 来源溯源", "pass", "消息 ID 为 Vertex 前缀，符合 Vertex AI 来源", "residue", 1.5, "claude_source_provenance")
        if bdrk:
            return evidence("Claude 来源溯源", "fail", f"声称 Vertex AI（Claude），但消息 ID 为 Bedrock 前缀（{mid[:32]}）", "residue", 4.0, "claude_source_provenance")
    elif source == "claude_official" and (bdrk or vrtx):
        return evidence("Claude 来源溯源", "fail", f"声称 Anthropic 官方，但消息 ID 带云厂前缀（{mid[:32]}）", "residue", 4.0, "claude_source_provenance")
    return evidence("Claude 来源溯源", "unknown", detail, "residue", 0.0, "claude_source_provenance")


def has_field(value, fields, depth=0):
    if depth > 12:
        return False
    if isinstance(value, dict):
        return bool(fields.intersection(value)) or any(has_field(v, fields, depth+1) for v in value.values())
    if isinstance(value, list):
        return any(has_field(v, fields, depth+1) for v in value)
    return False


def decide_verdict(valid, score, errored, claimed_source):
    """判定结论。有效检查不足或加权分母为 0 时不给结论；订阅类来源永不给 authentic。"""
    if not valid:
        return "error" if errored else "inconclusive"
    if len(valid) < MIN_SCORED_SIGNALS or score is None:
        return "inconclusive"
    if score >= AUTHENTIC_THRESHOLD:
        return "inconclusive" if claimed_source in UNPROVABLE_SOURCES else "authentic"
    if score >= SUSPECT_THRESHOLD:
        return "suspect"
    return "fail"


SUMMARY_TEXT = {
    "authentic": "协议与行为指纹与声称来源一致（非加密级，为概率判定）；未验证订阅身份或账单归属。",
    "inconclusive": "仅获得有限线索或输出不完整，不能证实实际来源。",
    "suspect": "部分指纹偏离声称来源，存在检查不一致，需人工复核。",
    "fail": "多项指纹与声称来源不符，判定来源不符（非加密级，为概率判定）。",
    "error": "探测失败，不能判断真实性。",
}


def record_for(base, target, ev, started, capability_accuracy=-1.0):
    valid = [e for e in ev if e["status"] in {"pass", "fail"}]
    score = weighted_score(valid)
    errored = any(e["status"] == "error" for e in ev)
    verdict = decide_verdict(valid, score, errored, target.get("claimed_source"))
    return {"id": uuid.uuid4().hex, "base_url": base, "model": target["model"], "family": target["family"],
            "claimed_source": target["claimed_source"], "source_label": SOURCE_MAP[target["claimed_source"]]["label"],
            "verdict": verdict, "score": score, "capability_accuracy": capability_accuracy,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": round((time.monotonic()-started)*1000), "evidence": ev,
            "summary": SUMMARY_TEXT[verdict]}


async def probe_target(base, key, target, timeout):
    started = time.monotonic()
    family, model = target["family"], target["model"]
    wire = target.get("wire_family", "gpt")
    prompt, answers = challenge()
    path = "messages" if wire == "claude" else "chat/completions" if wire == "gpt" else f"models/{model.removeprefix('models/')}:generateContent"
    url = endpoint(base, path, wire)
    body = probe_body(model, wire, prompt)
    ev = []
    stats = {}
    try:
        payload = await request_json("POST", url, key, body=body, timeout=timeout)
        ev = inspect_payload(payload, family, answers, wire, target.get("claimed_source"), stats)
        marker = has_field(payload, {"createTime", "amazon-bedrock-invocationMetrics", "service_tier", "system_fingerprint"})
        ev.append(evidence("来源字段线索", "unknown", "观察到供应商风格字段；可被网关复制，不能证实所选来源。" if marker else "未观察到可区分渠道来源的字段；字段缺失不证明来源不符。"))
        # One additional invalid-type request, never speculative model-specific restrictions.
        invalid = probe_body(model, wire, "Reply OK")
        if wire == "gemini":
            invalid["generationConfig"]["maxOutputTokens"] = "invalid-channel-probe"
        else:
            invalid["max_tokens" if wire == "claude" else "max_completion_tokens"] = "invalid-channel-probe"
        try:
            await request_json("POST", url, key, body=invalid, timeout=timeout)
            ev.append(evidence("参数校验", "unknown", "上游接受无效类型；可能网关清洗参数，不能单独证明掺假"))
        except ChannelError as exc:
            ev.append(evidence("参数校验", "unknown", "无效类型请求被拒绝；未保留错误回显，不能确认具体拒绝原因。" if exc.code == "upstream_error" else exc.message))
    except ChannelError as exc:
        ev.append(evidence("请求", "error", exc.message))
    except Exception:
        ev.append(evidence("请求", "error", "探针内部错误（详情不回显）"))
    finally:
        source = target["claimed_source"]
        detail = "公开协议、响应头、模型名均可仿造，未获得供应商凭证链。"
        if source in {"claude_ccmax", "claude_q_kiro", "claude_aws_partner"}:
            detail += "订阅/合作伙伴来源无法通过本 API 证明，必须另行核验授权和账单。"
        ev.append(evidence("来源证明", "unknown", detail))
    result = redact(record_for(base, target, ev, started, stats.get("capability_accuracy", -1.0)), key)
    key = ""
    return result
