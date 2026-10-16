"""本地假上游:模拟 OpenAI 兼容中转站的生图接口,离线自测用。

按模型名分流行为(便于验证"违约会被抓红"):
    good-image   尺寸/透明通道/响应结构全部守约
    bad-image    偷换尺寸(512x512) + transparent 请求丢 alpha + 编辑响应里没有图片
    good-chat    /v1/chat/completions 用 data URL 回图
    bad-chat     只回文字,不含图片
另外 pricing 里放两个"不该入选"的条目:text-only-model(image_output_price=0)、image-video-4k(名字含 image 但含 video)。
"""

from __future__ import annotations

import base64
import io
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw

GOOD_IMAGE = "good-image"
BAD_IMAGE = "bad-image"
GOOD_CHAT = "good-chat"
BAD_CHAT = "bad-chat"
TEXT_ONLY = "text-only-model"
VIDEO_MODEL = "image-video-4k"

PRICING = {
    "data": [
        {"model_name": GOOD_IMAGE, "supported_endpoint_types": ["image-generation"], "image_output_price": 0.02},
        {"model_name": BAD_IMAGE, "supported_endpoint_types": ["image-generation"], "image_output_price": 0.02},
        {"model_name": GOOD_CHAT, "supported_endpoint_types": ["chat"], "image_output_price": 0.01},
        {"model_name": BAD_CHAT, "supported_endpoint_types": ["chat"], "image_output_price": 0.01},
        {"model_name": TEXT_ONLY, "supported_endpoint_types": ["chat"], "image_output_price": 0},
        {"model_name": VIDEO_MODEL, "supported_endpoint_types": [], "image_output_price": 0},
    ]
}


def parse_size(text: str) -> tuple[int, int]:
    m = re.match(r"^(\d+)x(\d+)$", (text or "").strip().lower())
    if not m:
        return (1024, 1024)
    return (int(m.group(1)), int(m.group(2)))


def make_png(size_text: str, *, transparent: bool, force: tuple[int, int] | None = None) -> bytes:
    w, h = force or parse_size(size_text)
    img = Image.new("RGBA", (w, h), (255, 255, 255, 0)) if transparent else Image.new("RGB", (w, h), (240, 244, 250))
    draw = ImageDraw.Draw(img)
    box = [w // 4, h // 4, w * 3 // 4, h * 3 // 4]
    draw.ellipse(box, fill=(18, 133, 90, 255) if transparent else (18, 133, 90))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _b64_payload(png: bytes) -> dict:
    return {"created": 1, "data": [{"b64_json": base64.b64encode(png).decode("ascii")}]}


def _field(raw: bytes, name: str) -> str:
    m = re.search(rb'name="' + name.encode() + rb'"\r\n\r\n([^\r\n]+)', raw)
    return m.group(1).decode("utf-8", "replace") if m else ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        return

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/api/pricing":
            self._json(200, PRICING)
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        path = self.path.split("?")[0]

        if path == "/v1/images/generations":
            try:
                req = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                self._json(400, {"error": {"message": "bad json"}})
                return
            model = str(req.get("model") or "")
            size = str(req.get("size") or "1024x1024")
            background = str(req.get("background") or "auto")
            if model == BAD_IMAGE:
                self._json(200, _b64_payload(make_png(size, transparent=False, force=(512, 512))))
            else:
                self._json(200, _b64_payload(make_png(size, transparent=(background == "transparent"))))
            return

        if path in ("/v1/images/edits", "/v1/edits"):
            model = _field(raw, "model")
            size = _field(raw, "size") or "1024x1024"
            if model == BAD_IMAGE:
                self._json(200, {"created": 1, "data": [{}]})
            else:
                background = _field(raw, "background") or "auto"
                self._json(200, _b64_payload(make_png(size, transparent=(background == "transparent"))))
            return

        if path == "/v1/chat/completions":
            try:
                req = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                self._json(400, {"error": {"message": "bad json"}})
                return
            model = str(req.get("model") or "")
            if model == BAD_CHAT:
                self._json(200, {"choices": [{"message": {"content": "抱歉,我不会画画。"}}]})
                return
            png = make_png("1024x1024", transparent=False)
            data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
            self._json(200, {"choices": [{"message": {"content": [{"type": "image_url", "image_url": {"url": data_url}}]}}]})
            return

        self._json(404, {"error": {"message": "not found"}})


def start(port: int = 0) -> tuple[ThreadingHTTPServer, int]:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


if __name__ == "__main__":
    srv, p = start(8099)
    print(f"fake upstream on http://127.0.0.1:{p}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
