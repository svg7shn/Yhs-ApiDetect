"""生图模型检测引擎 —— /image 页面的判据层。

原先这套检测由一个独立的 Go 服务(image-param-verifier)承担,本站只做 iframe 代理。
现在把判据在站内用 Python 重写,行为逐项对齐,不再需要外部进程,也没有账号/session:

* 六项测试(文生图 transparent/opaque/auto、改图 /v1/images/edits、改图 /v1/edits、
  聊天生图 /v1/chat/completions)各产出一条 CheckResult;请求 JSON、响应原文、
  生成的图片全部落盘成证据,前端按 phase 逐项回看。
* 每条 CheckResult 由若干 CheckDetail 组成(保存请求 / HTTP 请求 / 保存响应 /
  HTTP 状态码 / 解析响应 JSON / 接口错误字段 / 解码图片 / 保存图片 / 尺寸 /
  格式 / 透明),失败的项汇总成 reason。
* 取图三形态都支持:b64_json、url 下载、chat 响应里嵌的 data URL / Markdown 图片。

API Key 只活在本次请求的内存里:不落盘、不进日志、不写进证据文件。
"""

from __future__ import annotations

import base64
import io
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://www.yhshu.ai"
PRICING_PATH = "/api/pricing"
PRICING_TIMEOUT_S = 30.0
DEFAULT_TIMEOUT_S = 180.0
DEFAULT_PROMPT = "A cute cat sitting on a sofa"
DEFAULT_EDIT_PROMPT = "Change the sofa color to blue, keep the cat and composition unchanged"
DEFAULT_OUTPUT_FORMAT = "png"
DEFAULT_N = 1

PHASE_CHAT = "chat_generation"
PHASE_GEN_TRANSPARENT = "generation_transparent"
PHASE_GEN_OPAQUE = "generation_opaque"
PHASE_GEN_AUTO = "generation_auto"
PHASE_EDIT_IMAGES = "edit_images_edits"
PHASE_EDIT_ALIAS = "edit_alias_edits"
# 只在「仅勾选改图」时出现：自动补跑一次文生图当编辑输入。
PHASE_EDIT_SOURCE = "edit_source"

BACKGROUNDS: tuple[str, ...] = ("transparent", "opaque", "auto")

# 改图的两条路径,顺序即前端展示顺序。
EDIT_PATHS: tuple[tuple[str, str], ...] = (
    (PHASE_EDIT_IMAGES, "/v1/images/edits"),
    (PHASE_EDIT_ALIAS, "/v1/edits"),
)


@dataclass(frozen=True)
class PhaseSpec:
    name: str
    label: str
    kind: str  # images | chat

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "label": self.label, "kind": self.kind}


# 顺序即前端勾选框顺序。
PHASE_INFO: tuple[PhaseSpec, ...] = (
    PhaseSpec(PHASE_GEN_TRANSPARENT, "文生图 · transparent", "images"),
    PhaseSpec(PHASE_GEN_OPAQUE, "文生图 · opaque", "images"),
    PhaseSpec(PHASE_GEN_AUTO, "文生图 · auto", "images"),
    PhaseSpec(PHASE_EDIT_IMAGES, "改图 · /v1/images/edits", "images"),
    PhaseSpec(PHASE_EDIT_ALIAS, "改图 · /v1/edits", "images"),
    PhaseSpec(PHASE_CHAT, "聊天生图 · /v1/chat/completions", "chat"),
)

PHASE_LABELS: dict[str, str] = {p.name: p.label for p in PHASE_INFO}
PHASE_LABELS[PHASE_EDIT_SOURCE] = "文生图 · 自动原图"

IMAGE_NAME_KEYWORDS: tuple[str, ...] = (
    "image",
    "imagine",
    "banana",
    "seedream",
    "dall-e",
    "dalle",
)

_SIZE_RE = re.compile(r"^\d+x\d+$")
_K_SIZE_RE = re.compile(r"^\d+k$")
_DATA_URL_RE = re.compile(r"data:image/(png|jpeg|jpg|webp|gif);base64,([A-Za-z0-9+/=\s]+)")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^\s\)]+)\)")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class ModelSpec:
    """一个待测模型。kind 决定走哪条 API 路径。"""

    name: str
    kind: str = "images"
    size: str = "1024x1024"

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "kind": self.kind, "size": self.size}


@dataclass
class CheckDetail:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class CheckResult:
    model: str
    phase: str
    passed: bool = False
    reason: str = ""
    checks: list[CheckDetail] = field(default_factory=list)
    request_file: str = ""
    response_file: str = ""
    image_file: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "phase": self.phase,
            "phase_label": PHASE_LABELS.get(self.phase, self.phase),
            "passed": self.passed,
            "reason": self.reason,
            "checks": [c.to_dict() for c in self.checks],
            "request_file": self.request_file,
            "response_file": self.response_file,
            "image_file": self.image_file,
        }


@dataclass
class ProbeConfig:
    """一次检测的运行配置。api_key 只在内存中传递。"""

    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    retry: int = 0

    def normalized(self) -> ProbeConfig:
        base = normalize_base_url(self.base_url)
        timeout = self.timeout_s if self.timeout_s and self.timeout_s > 0 else DEFAULT_TIMEOUT_S
        retry = max(0, int(self.retry or 0))
        return ProbeConfig(base_url=base, api_key=self.api_key, timeout_s=float(timeout), retry=retry)


def normalize_base_url(raw: str) -> str:
    """把用户填的地址规范化成 https://host 形式(去掉尾部 / 与 /v1)。"""
    url = (raw or "").strip().rstrip("/")
    if not url:
        return DEFAULT_BASE_URL
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    url = url.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3].rstrip("/")
    return url or DEFAULT_BASE_URL


# ---------------------------------------------------------------------------
# 模型发现与规范化
# ---------------------------------------------------------------------------


def detect_kind(endpoint_types: list[str] | None) -> str:
    return "images" if "image-generation" in (endpoint_types or []) else "chat"


def detect_size(name: str) -> str:
    lower = (name or "").lower()
    if "4k" in lower:
        return "4096x4096"
    if "2k" in lower:
        return "2048x2048"
    return "1024x1024"


def is_image_capable_model(entry: dict[str, Any]) -> bool:
    """pricing 条目是否具备图片能力(三条判据满足其一)。"""
    types = entry.get("supported_endpoint_types") or []
    if isinstance(types, list) and "image-generation" in types:
        return True
    try:
        if float(entry.get("image_output_price") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    lower = str(entry.get("model_name") or "").lower()
    if not any(kw in lower for kw in IMAGE_NAME_KEYWORDS):
        return False
    return "video" not in lower


def parse_pricing(payload: Any) -> list[ModelSpec]:
    """把 /api/pricing 的响应转成去重后的图片模型列表。"""
    if not isinstance(payload, dict):
        raise ValueError("pricing 响应不是 JSON 对象")
    out: list[ModelSpec] = []
    seen: set[str] = set()
    for entry in payload.get("data") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("model_name") or "").strip()
        if not name or name in seen:
            continue
        if not is_image_capable_model(entry):
            continue
        seen.add(name)
        out.append(
            ModelSpec(
                name=name,
                kind=detect_kind(entry.get("supported_endpoint_types")),
                size=detect_size(name),
            )
        )
    return out


async def fetch_image_models(cfg: ProbeConfig, client: httpx.AsyncClient) -> list[ModelSpec]:
    """拉取 pricing 列表;失败抛 RuntimeError,文案与旧实现一致。"""
    url = cfg.base_url.rstrip("/") + PRICING_PATH
    try:
        resp = await client.get(url, timeout=PRICING_TIMEOUT_S)
    except Exception as exc:
        raise RuntimeError(f"请求 pricing 接口失败: {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(f"pricing 接口 HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except Exception as exc:
        raise RuntimeError(f"解析 pricing JSON 失败: {exc}") from exc
    return parse_pricing(payload)


def split_model_names(text: str) -> list[str]:
    """模型名支持逗号/分号/顿号/换行分隔。"""
    parts = re.split(r"[,;\n\r，、；]", text or "")
    return [p.strip() for p in parts if p.strip()]


def normalize_specs(raw_specs: list[dict[str, Any]] | None) -> list[ModelSpec]:
    """补齐用户手填的模型信息:kind 默认 images,size 按名称推断或取默认。"""
    out: list[ModelSpec] = []
    seen: set[str] = set()
    for item in raw_specs or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        kind = "chat" if str(item.get("kind") or "").strip().lower() == "chat" else "images"
        size = str(item.get("size") or "").strip().lower()
        if not _SIZE_RE.match(size):
            size = detect_size(size) if (size or _K_SIZE_RE.match(size)) else detect_size(name)
        seen.add(name)
        out.append(ModelSpec(name=name, kind=kind, size=size))
    return out


async def resolve_models(
    cfg: ProbeConfig,
    client: httpx.AsyncClient,
    selected: list[dict[str, Any]] | None = None,
    manual: list[str] | None = None,
) -> list[ModelSpec]:
    """选模型优先级:用户勾选 > 手填模型名 > 自动拉取全部图片模型。"""
    specs = normalize_specs(selected)
    if specs:
        return specs
    manual_names = [n for n in (manual or []) if str(n).strip()]
    if manual_names:
        return normalize_specs([{"name": n} for n in manual_names])
    fetched = await fetch_image_models(cfg, client)
    if not fetched:
        raise RuntimeError("接口未返回具备图片能力的模型(可手动填写模型名)")
    return fetched


def normalize_phases(raw: list[str] | None) -> set[str]:
    """把勾选的测试项转成集合;空集合表示全部。未知项报错,避免静默跑成全部。"""
    known = {p.name for p in PHASE_INFO} | {PHASE_EDIT_SOURCE}
    out: set[str] = set()
    for name in raw or []:
        name = str(name).strip()
        if not name:
            continue
        if name not in known:
            raise ValueError(f"未知的测试项: {name}")
        out.add(name)
    return out


def want_phase(phases: set[str], name: str) -> bool:
    return True if not phases else name in phases


def has_applicable_phase(spec: ModelSpec, phases: set[str]) -> bool:
    if spec.kind == "chat":
        return want_phase(phases, PHASE_CHAT)
    return any(
        want_phase(phases, name)
        for name in (
            PHASE_GEN_TRANSPARENT,
            PHASE_GEN_OPAQUE,
            PHASE_GEN_AUTO,
            PHASE_EDIT_IMAGES,
            PHASE_EDIT_ALIAS,
        )
    )


def selection_mismatch(specs: list[ModelSpec], phases: set[str]) -> str | None:
    """模型类型与测试项对不上时报错,文案与旧实现一致。"""
    if not specs:
        return "没有可测试的模型"
    usable = [s for s in specs if has_applicable_phase(s, phases)]
    if usable:
        return None
    return "所选模型与测试项不匹配：chat 类模型只能测「聊天生图」，images 类模型只能测文生图/改图"


# ---------------------------------------------------------------------------
# HTTP 辅助
# ---------------------------------------------------------------------------


def _auth_headers(cfg: ProbeConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {cfg.api_key}"}


async def _post_json(
    cfg: ProbeConfig, client: httpx.AsyncClient, url: str, payload: bytes
) -> tuple[int | None, bytes, str | None]:
    headers = dict(_auth_headers(cfg))
    headers["Content-Type"] = "application/json"
    try:
        resp = await client.post(url, content=payload, headers=headers, timeout=cfg.timeout_s)
    except Exception as exc:  # noqa: BLE001
        return None, b"", str(exc) or type(exc).__name__
    return resp.status_code, resp.content, None


async def _post_multipart(
    cfg: ProbeConfig,
    client: httpx.AsyncClient,
    url: str,
    fields: dict[str, str],
    image_name: str,
    image_bytes: bytes,
) -> tuple[int | None, bytes, str | None]:
    files = {"image": (image_name, image_bytes, "application/octet-stream")}
    try:
        resp = await client.post(
            url,
            data=fields,
            files=files,
            headers=_auth_headers(cfg),
            timeout=cfg.timeout_s,
        )
    except Exception as exc:  # noqa: BLE001
        return None, b"", str(exc) or type(exc).__name__
    return resp.status_code, resp.content, None


# ---------------------------------------------------------------------------
# 取图:响应解码 / URL 下载 / chat 提取
# ---------------------------------------------------------------------------


def _decode_bytes(raw: bytes) -> tuple[Image.Image, str]:
    img = Image.open(io.BytesIO(raw))
    img.load()
    return img, (img.format or "").lower()


async def _decode_image_url(
    cfg: ProbeConfig, client: httpx.AsyncClient, url: str
) -> tuple[Image.Image, str]:
    if url.startswith("data:"):
        match = _DATA_URL_RE.match(url)
        if not match:
            raise ValueError("data URL 无法解析")
        return _decode_bytes(base64.b64decode(re.sub(r"\s+", "", match.group(2))))
    try:
        resp = await client.get(url, timeout=cfg.timeout_s)
    except Exception as exc:
        raise ValueError(f"下载图片失败: {exc}") from exc
    if resp.status_code != 200:
        raise ValueError(f"下载图片失败，HTTP {resp.status_code}")
    return _decode_bytes(resp.content)


def _extract_image_from_string(text: str) -> str | None:
    """返回字符串里找到的图片(data URL 或 http 链接),找不到返回 None。"""
    match = _DATA_URL_RE.search(text or "")
    if match:
        return "data:image/" + match.group(1) + ";base64," + re.sub(r"\s+", "", match.group(2))
    match = _MD_IMAGE_RE.search(text or "")
    if match:
        return match.group(1)
    return None


async def _extract_image_from_content(
    cfg: ProbeConfig, client: httpx.AsyncClient, content: Any
) -> tuple[Image.Image, str]:
    if isinstance(content, str):
        candidate = _extract_image_from_string(content)
        if not candidate:
            raise ValueError("字符串中未找到图片")
        return await _decode_image_url(cfg, client, candidate)
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "image_url":
                url = (item.get("image_url") or {}).get("url") if isinstance(item.get("image_url"), dict) else None
                if url:
                    return await _decode_image_url(cfg, client, str(url))
            text = item.get("text")
            if isinstance(text, str) and text:
                candidate = _extract_image_from_string(text)
                if candidate:
                    try:
                        return await _decode_image_url(cfg, client, candidate)
                    except Exception:  # noqa: BLE001 — 与旧实现一致:继续找下一项
                        continue
    raise ValueError("content 中未找到图片")


async def decode_image_from_response(
    cfg: ProbeConfig, client: httpx.AsyncClient, payload: dict[str, Any]
) -> tuple[Image.Image, str]:
    data = payload.get("data") or []
    if not data:
        raise ValueError("响应 data 为空")
    item = data[0] if isinstance(data[0], dict) else {}
    b64 = str(item.get("b64_json") or "").strip()
    if b64:
        return _decode_bytes(base64.b64decode(b64))
    url = str(item.get("url") or "").strip()
    if url:
        return await _decode_image_url(cfg, client, url)
    raise ValueError("响应中既没有 b64_json 也没有 url")


async def decode_image_from_chat(
    cfg: ProbeConfig, client: httpx.AsyncClient, payload: dict[str, Any]
) -> tuple[Image.Image, str]:
    """chat 响应取图:先认 data 数组,再认 message.images[],最后挖 content 文本。"""
    if isinstance(payload.get("data"), list) and payload["data"]:
        return await decode_image_from_response(cfg, client, payload)
    choices = payload.get("choices") or []
    if not choices:
        raise ValueError("响应不含 choices")
    message = (choices[0] or {}).get("message") or {}
    for item in message.get("images") or []:
        if isinstance(item, dict):
            url = (item.get("image_url") or {}).get("url") if isinstance(item.get("image_url"), dict) else None
            if url:
                return await _decode_image_url(cfg, client, str(url))
    return await _extract_image_from_content(cfg, client, message.get("content"))


# ---------------------------------------------------------------------------
# 判据:尺寸 / 格式 / 透明
# ---------------------------------------------------------------------------


def parse_size(text: str) -> tuple[int, int]:
    parts = (text or "").strip().lower().split("x")
    if len(parts) != 2:
        raise ValueError(f"无效尺寸: {text}")
    try:
        return int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"无效尺寸: {text}") from exc


def has_transparent_pixel(img: Image.Image) -> bool:
    """是否存在 alpha < 255 的像素(等价于旧实现逐像素找 alpha<65535)。"""
    try:
        alpha = img.convert("RGBA").getchannel("A")
    except Exception:  # noqa: BLE001 — 极端色彩模式下退化为「不透明」
        return False
    return alpha.getextrema()[0] < 255


def check_image(
    img: Image.Image, expect_size: str, expect_format: str, background: str, actual_format: str
) -> list[CheckDetail]:
    checks: list[CheckDetail] = []

    width, height = img.size
    try:
        want_w, want_h = parse_size(expect_size)
    except ValueError as exc:
        checks.append(CheckDetail("尺寸", False, f"期望尺寸解析失败: {exc}"))
    else:
        if (width, height) == (want_w, want_h):
            checks.append(CheckDetail("尺寸", True, f"{width}x{height}"))
        else:
            checks.append(CheckDetail("尺寸", False, f"期望 {want_w}x{want_h}，实际 {width}x{height}"))

    if expect_format:
        if actual_format == expect_format.lower():
            checks.append(CheckDetail("格式", True, actual_format))
        else:
            checks.append(CheckDetail("格式", False, f"期望 {expect_format}，实际 {actual_format}"))
    else:
        checks.append(CheckDetail("格式", True, f"未指定期望格式，实际 {actual_format}"))

    transparent = has_transparent_pixel(img)
    if background == "transparent":
        checks.append(CheckDetail("透明", transparent, f"请求 transparent，实际存在透明像素={transparent}"))
    elif background == "opaque":
        passed = not transparent
        checks.append(CheckDetail("透明", passed, f"请求 opaque，实际存在透明像素={transparent}"))
    elif background == "auto":
        checks.append(CheckDetail("透明", True, f"请求 auto，实际存在透明像素={transparent}"))
    else:
        checks.append(CheckDetail("透明", False, f"未知 background: {background}"))

    return checks


# ---------------------------------------------------------------------------
# 证据落盘
# ---------------------------------------------------------------------------


def _sanitize_dir(name: str) -> str:
    """模型名会作为证据子目录名,先清洗再拼接,避免路径穿越。"""
    cleaned = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]", "_", (name or "").strip())
    cleaned = cleaned.strip("._")
    return (cleaned or "model")[:80]


def save_png(path: Path, img: Image.Image) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = img
    if out.mode not in ("RGBA", "RGB", "LA", "L", "P"):
        out = out.convert("RGBA")
    out.save(path, format="PNG")


def _write_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_prompt_text(spec: ModelSpec, gen_prompt: str, edit_prompt: str) -> str:
    return (
        f"模型: {spec.name}\n"
        f"类型: {spec.kind}\n"
        f"期望尺寸: {spec.size}\n"
        f"输出格式: {DEFAULT_OUTPUT_FORMAT}\n\n"
        f"文生图提示词:\n{gen_prompt}\n\n"
        f"编辑提示词:\n{edit_prompt}\n"
    )


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def collect_fail_reasons(checks: list[CheckDetail]) -> list[str]:
    return [f"{c.name}：{c.detail}" for c in checks if not c.passed]


# ---------------------------------------------------------------------------
# 六项测试
# ---------------------------------------------------------------------------


async def do_generation(
    cfg: ProbeConfig,
    client: httpx.AsyncClient,
    spec: ModelSpec,
    model_dir: Path,
    background: str,
) -> CheckResult:
    phase = "generation_" + background
    res = CheckResult(
        model=spec.name,
        phase=phase,
        request_file=str(model_dir / f"generation_{background}_request.json"),
        response_file=str(model_dir / f"generation_{background}_response.json"),
        image_file=str(model_dir / f"original_{background}.{DEFAULT_OUTPUT_FORMAT}"),
    )
    request = {
        "model": spec.name,
        "prompt": DEFAULT_PROMPT,
        "n": DEFAULT_N,
        "size": spec.size,
        "background": background,
        "output_format": DEFAULT_OUTPUT_FORMAT,
    }
    body = json.dumps(request, ensure_ascii=False, indent=2).encode("utf-8")
    req_path, resp_path, img_path = Path(res.request_file), Path(res.response_file), Path(res.image_file)
    try:
        _write_bytes(req_path, body)
    except OSError as exc:
        res.reason = f"保存请求 JSON 失败: {exc}"
        res.checks.append(CheckDetail("保存请求", False, str(exc)))
        return res
    res.checks.append(CheckDetail("保存请求", True, str(req_path)))

    status, raw, err = await _post_json(
        cfg, client, cfg.base_url + "/v1/images/generations", body
    )
    if err is not None:
        res.reason = f"请求失败: {err}"
        res.checks.append(CheckDetail("HTTP 请求", False, err))
        return res
    _write_bytes(resp_path, raw)
    res.checks.append(CheckDetail("HTTP 请求", True, f"HTTP {status}"))
    res.checks.append(CheckDetail("保存响应", True, str(resp_path)))

    if status != 200:
        res.reason = f"HTTP 状态码异常: {status}，响应: {_truncate(raw.decode('utf-8', 'replace'), 500)}"
        res.checks.append(CheckDetail("HTTP 状态码", False, str(status)))
        return res
    res.checks.append(CheckDetail("HTTP 状态码", True, "200"))

    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
    except Exception as exc:  # noqa: BLE001
        res.reason = f"解析响应 JSON 失败: {exc}"
        res.checks.append(CheckDetail("解析响应 JSON", False, str(exc)))
        return res
    if not isinstance(payload, dict):
        res.reason = "解析响应 JSON 失败: 顶层不是对象"
        res.checks.append(CheckDetail("解析响应 JSON", False, "顶层不是对象"))
        return res
    res.checks.append(CheckDetail("解析响应 JSON", True, "成功"))

    api_error = payload.get("error")
    if api_error:
        message = str(api_error.get("message") if isinstance(api_error, dict) else api_error)
        res.reason = f"接口返回错误: {message}"
        res.checks.append(CheckDetail("接口错误字段", False, message))
        return res
    res.checks.append(CheckDetail("接口错误字段", True, "无错误"))

    try:
        img, actual_format = await decode_image_from_response(cfg, client, payload)
    except Exception as exc:  # noqa: BLE001
        res.reason = f"解码图片失败: {exc}"
        res.checks.append(CheckDetail("解码图片", False, str(exc)))
        return res
    res.checks.append(CheckDetail("解码图片", True, actual_format or "unknown"))

    try:
        save_png(img_path, img)
    except Exception as exc:  # noqa: BLE001
        res.reason = f"保存图片失败: {exc}"
        res.checks.append(CheckDetail("保存图片", False, str(exc)))
        return res
    res.checks.append(CheckDetail("保存图片", True, str(img_path)))

    checks = check_image(img, spec.size, DEFAULT_OUTPUT_FORMAT, background, actual_format)
    res.checks.extend(checks)
    reasons = collect_fail_reasons(checks)
    if reasons:
        res.reason = "；".join(reasons)
        return res
    res.passed = True
    res.reason = "全部检查通过"
    return res


async def do_edit(
    cfg: ProbeConfig,
    client: httpx.AsyncClient,
    spec: ModelSpec,
    model_dir: Path,
    source_image: Path,
    source_background: str,
    phase: str,
    path: str,
) -> CheckResult:
    res = CheckResult(
        model=spec.name,
        phase=phase,
        request_file=str(model_dir / f"{phase}_request.json"),
        response_file=str(model_dir / f"{phase}_response.json"),
        image_file=str(model_dir / f"edited_{phase}.{DEFAULT_OUTPUT_FORMAT}"),
    )
    request = {
        "model": spec.name,
        "prompt": DEFAULT_EDIT_PROMPT,
        "n": DEFAULT_N,
        "size": spec.size,
        "background": source_background,
        "output_format": DEFAULT_OUTPUT_FORMAT,
        "image": str(source_image),
        "path": path,
    }
    body = json.dumps(request, ensure_ascii=False, indent=2).encode("utf-8")
    req_path, resp_path, img_path = Path(res.request_file), Path(res.response_file), Path(res.image_file)
    try:
        _write_bytes(req_path, body)
    except OSError as exc:
        res.reason = f"保存请求 JSON 失败: {exc}"
        res.checks.append(CheckDetail("保存请求", False, str(exc)))
        return res
    res.checks.append(CheckDetail("保存请求", True, str(req_path)))

    try:
        image_bytes = source_image.read_bytes()
    except OSError as exc:
        res.reason = f"读取原图失败: {exc}"
        res.checks.append(CheckDetail("读取原图", False, str(exc)))
        return res

    fields = {
        "model": spec.name,
        "prompt": DEFAULT_EDIT_PROMPT,
        "n": str(DEFAULT_N),
        "size": spec.size,
        "background": source_background,
        "output_format": DEFAULT_OUTPUT_FORMAT,
    }
    status, raw, err = await _post_multipart(
        cfg, client, cfg.base_url + path, fields, source_image.name, image_bytes
    )
    if err is not None:
        res.reason = f"请求失败: {err}"
        res.checks.append(CheckDetail("HTTP 请求", False, err))
        return res
    _write_bytes(resp_path, raw)
    res.checks.append(CheckDetail("HTTP 请求", True, f"HTTP {status}"))
    res.checks.append(CheckDetail("保存响应", True, str(resp_path)))

    if status != 200:
        res.reason = f"HTTP 状态码异常: {status}，响应: {_truncate(raw.decode('utf-8', 'replace'), 500)}"
        res.checks.append(CheckDetail("HTTP 状态码", False, str(status)))
        return res
    res.checks.append(CheckDetail("HTTP 状态码", True, "200"))

    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
    except Exception as exc:  # noqa: BLE001
        res.reason = f"解析响应 JSON 失败: {exc}"
        res.checks.append(CheckDetail("解析响应 JSON", False, str(exc)))
        return res
    if not isinstance(payload, dict):
        res.reason = "解析响应 JSON 失败: 顶层不是对象"
        res.checks.append(CheckDetail("解析响应 JSON", False, "顶层不是对象"))
        return res
    res.checks.append(CheckDetail("解析响应 JSON", True, "成功"))

    api_error = payload.get("error")
    if api_error:
        message = str(api_error.get("message") if isinstance(api_error, dict) else api_error)
        res.reason = f"接口返回错误: {message}"
        res.checks.append(CheckDetail("接口错误字段", False, message))
        return res
    res.checks.append(CheckDetail("接口错误字段", True, "无错误"))

    try:
        img, actual_format = await decode_image_from_response(cfg, client, payload)
    except Exception as exc:  # noqa: BLE001
        res.reason = f"解码图片失败: {exc}"
        res.checks.append(CheckDetail("解码图片", False, str(exc)))
        return res
    res.checks.append(CheckDetail("解码图片", True, actual_format or "unknown"))

    try:
        save_png(img_path, img)
    except Exception as exc:  # noqa: BLE001
        res.reason = f"保存图片失败: {exc}"
        res.checks.append(CheckDetail("保存图片", False, str(exc)))
        return res
    res.checks.append(CheckDetail("保存图片", True, str(img_path)))

    checks = check_image(img, spec.size, DEFAULT_OUTPUT_FORMAT, source_background, actual_format)
    res.checks.extend(checks)
    reasons = collect_fail_reasons(checks)
    if reasons:
        res.reason = "；".join(reasons)
        return res
    res.passed = True
    res.reason = "全部检查通过，编辑结果请人工审核"
    return res


async def do_chat_generation(
    cfg: ProbeConfig, client: httpx.AsyncClient, spec: ModelSpec, model_dir: Path
) -> CheckResult:
    res = CheckResult(
        model=spec.name,
        phase=PHASE_CHAT,
        request_file=str(model_dir / "chat_generation_request.json"),
        response_file=str(model_dir / "chat_generation_response.json"),
        image_file=str(model_dir / f"chat_generated.{DEFAULT_OUTPUT_FORMAT}"),
    )
    request = {
        "model": spec.name,
        "messages": [{"role": "user", "content": DEFAULT_PROMPT}],
        "stream": False,
    }
    body = json.dumps(request, ensure_ascii=False, indent=2).encode("utf-8")
    req_path, resp_path, img_path = Path(res.request_file), Path(res.response_file), Path(res.image_file)
    try:
        _write_bytes(req_path, body)
    except OSError as exc:
        res.reason = f"保存请求 JSON 失败: {exc}"
        res.checks.append(CheckDetail("保存请求", False, str(exc)))
        return res
    res.checks.append(CheckDetail("保存请求", True, str(req_path)))

    status, raw, err = await _post_json(cfg, client, cfg.base_url + "/v1/chat/completions", body)
    if err is not None:
        res.reason = f"请求失败: {err}"
        res.checks.append(CheckDetail("HTTP 请求", False, err))
        return res
    _write_bytes(resp_path, raw)
    res.checks.append(CheckDetail("HTTP 请求", True, f"HTTP {status}"))
    res.checks.append(CheckDetail("保存响应", True, str(resp_path)))

    if status != 200:
        res.reason = f"HTTP 状态码异常: {status}，响应: {_truncate(raw.decode('utf-8', 'replace'), 500)}"
        res.checks.append(CheckDetail("HTTP 状态码", False, str(status)))
        return res
    res.checks.append(CheckDetail("HTTP 状态码", True, "200"))

    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
    except Exception as exc:  # noqa: BLE001
        res.reason = f"解析响应 JSON 失败: {exc}"
        res.checks.append(CheckDetail("解析响应 JSON", False, str(exc)))
        return res
    if not isinstance(payload, dict):
        res.reason = "解析响应 JSON 失败: 顶层不是对象"
        res.checks.append(CheckDetail("解析响应 JSON", False, "顶层不是对象"))
        return res
    res.checks.append(CheckDetail("解析响应 JSON", True, "成功"))

    api_error = payload.get("error")
    if api_error:
        message = str(api_error.get("message") if isinstance(api_error, dict) else api_error)
        res.reason = f"接口返回错误: {message}"
        res.checks.append(CheckDetail("接口错误字段", False, message))
        return res
    res.checks.append(CheckDetail("接口错误字段", True, "无错误"))

    try:
        img, actual_format = await decode_image_from_chat(cfg, client, payload)
    except Exception as exc:  # noqa: BLE001
        res.reason = f"提取图片失败: {exc}"
        res.checks.append(CheckDetail("提取图片", False, str(exc)))
        return res
    res.checks.append(CheckDetail("提取图片", True, actual_format or "unknown"))

    try:
        save_png(img_path, img)
    except Exception as exc:  # noqa: BLE001
        res.reason = f"保存图片失败: {exc}"
        res.checks.append(CheckDetail("保存图片", False, str(exc)))
        return res
    res.checks.append(CheckDetail("保存图片", True, str(img_path)))

    # chat 生图没有 background 参数,透明度只记录不作判据。
    checks = check_image(img, spec.size, DEFAULT_OUTPUT_FORMAT, "auto", actual_format)
    res.checks.extend(checks)
    reasons = collect_fail_reasons(checks)
    if reasons:
        res.reason = "；".join(reasons)
        return res
    res.passed = True
    res.reason = "全部检查通过"
    return res


# ---------------------------------------------------------------------------
# 调度:一个模型跑完它该跑的项目
# ---------------------------------------------------------------------------


async def _with_retry(cfg: ProbeConfig, factory: Callable[[], Awaitable[CheckResult]]) -> CheckResult:
    attempts = cfg.retry + 1
    last: CheckResult | None = None
    for _ in range(attempts):
        last = await factory()
        if last.passed:
            return last
    assert last is not None
    return last


async def test_model(
    cfg: ProbeConfig,
    client: httpx.AsyncClient,
    spec: ModelSpec,
    phases: set[str],
    root_dir: Path,
    publish: Callable[[CheckResult], None] | None = None,
) -> list[CheckResult]:
    """跑完一个模型的全部选中项,逐条回调 publish。"""
    results: list[CheckResult] = []
    model_dir = root_dir / _sanitize_dir(spec.name)

    def emit(res: CheckResult) -> None:
        if not res.model:
            res.model = spec.name
        results.append(res)
        if publish is not None:
            publish(res)

    if spec.kind == "chat":
        if not want_phase(phases, PHASE_CHAT):
            return results
        emit(await _with_retry(cfg, lambda: do_chat_generation(cfg, client, spec, model_dir)))
        return results

    need_edit = want_phase(phases, PHASE_EDIT_IMAGES) or want_phase(phases, PHASE_EDIT_ALIAS)
    any_gen_selected = any(
        want_phase(phases, name)
        for name in (PHASE_GEN_TRANSPARENT, PHASE_GEN_OPAQUE, PHASE_GEN_AUTO)
    )
    # 只勾改图时,仍然需要一张原图,这里自动补跑一次 transparent 生成。
    need_auto_source = need_edit and not any_gen_selected

    original_by_bg: dict[str, Path] = {}

    for background in BACKGROUNDS:
        phase = "generation_" + background
        forced_source = False
        if not want_phase(phases, phase):
            if not (need_auto_source and background == "transparent"):
                continue
            forced_source = True

        res = await _with_retry(
            cfg, lambda bg=background: do_generation(cfg, client, spec, model_dir, bg)
        )
        if forced_source:
            res.phase = PHASE_EDIT_SOURCE
            if res.passed:
                res.reason = "自动生成原图(仅勾选改图测试,作为编辑输入)"
            else:
                res.reason = "自动生成原图失败(改图测试的前置输入): " + res.reason
        emit(res)

        image_path = Path(res.image_file)
        if image_path.exists():
            original_by_bg[background] = image_path

    if not need_edit:
        return results

    source_image = original_by_bg.get("transparent") or next(iter(original_by_bg.values()), None)
    source_background = "transparent" if "transparent" in original_by_bg else next(iter(original_by_bg), "")

    if source_image is None:
        phase = next(
            (name for name, _ in EDIT_PATHS if want_phase(phases, name)), PHASE_EDIT_IMAGES
        )
        emit(
            CheckResult(
                model=spec.name,
                phase=phase,
                passed=False,
                reason="没有可用的原图，无法进行编辑测试",
                checks=[CheckDetail("前置条件", False, "文生图阶段没有生成图片")],
            )
        )
        return results

    (model_dir / "prompt.txt").parent.mkdir(parents=True, exist_ok=True)
    (model_dir / "prompt.txt").write_text(
        build_prompt_text(spec, DEFAULT_PROMPT, DEFAULT_EDIT_PROMPT), encoding="utf-8"
    )

    for phase, path in EDIT_PATHS:
        if not want_phase(phases, phase):
            continue
        emit(
            await _with_retry(
                cfg,
                lambda p=phase, u=path: do_edit(
                    cfg, client, spec, model_dir, source_image, source_background, p, u
                ),
            )
        )

    return results
