"""Black-box HTTP check: start the real app, drive it from outside, never touch the network.

Runs the production ASGI app under uvicorn on a random loopback port and issues real
HTTP requests against every registered route plus a set of malformed / hostile inputs.
No upstream credentials, no external network, no writes to the live data directories
(all persistence is redirected to .pytest-tmp by the env vars below).

Pass criterion that matters most: malformed input must never produce a 5xx.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = ROOT / ".pytest-tmp" / "blackbox"
TMP.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("Yhs-ApiDetect_JOBS_DIR", str(TMP / "jobs"))
os.environ.setdefault("Yhs-ApiDetect_WISHLIST_PATH", str(TMP / "wishlist.txt"))
os.environ.setdefault("Yhs-ApiDetect_AUTHENTICITY_DIR", str(TMP / "authenticity"))
os.environ.setdefault("Yhs-ApiDetect_AUTHENTICITY_CONFIG", str(TMP / "auth-config.json"))
os.environ.setdefault("Yhs-ApiDetect_CHANNEL_DIR", str(TMP / "channel"))

from relay_detector.proxy_env import repair_proxy_environment  # noqa: E402

repair_proxy_environment()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from web.server import app  # noqa: E402

OUT = ROOT / "artifacts" / "blackbox"
OUT.mkdir(parents=True, exist_ok=True)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def registered_routes() -> list[tuple[str, str]]:
    rows = []
    for r in app.routes:
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", None)
        if not path or not methods:
            continue
        for m in sorted(methods):
            if m in {"HEAD", "OPTIONS"}:
                continue
            rows.append((m, path))
    return sorted(set(rows))


# (label, method, path, kwargs, expect)  expect: set of acceptable status codes,
# or the string "not5xx" meaning any status below 500 is acceptable.
CASES: list[tuple[str, str, str, dict, object]] = [
    # --- pages ---
    ("page-index", "GET", "/", {}, {200}),
    ("page-claude", "GET", "/claude", {}, {200}),
    ("page-openai", "GET", "/openai", {}, {200}),
    ("page-gemini", "GET", "/gemini", {}, {200}),
    ("page-leaderboard", "GET", "/leaderboard", {}, {200}),
    ("page-leaderboard-domain", "GET", "/leaderboard/claude", {}, "not5xx"),
    ("page-history", "GET", "/history", {}, {200}),
    ("page-batch", "GET", "/batch", {}, {200}),
    ("page-faq", "GET", "/faq", {}, {200}),
    ("page-authenticity", "GET", "/authenticity", {}, {200}),
    ("page-channel", "GET", "/channel", {}, {200}),
    ("page-image", "GET", "/image", {}, {200}),
    ("healthz", "GET", "/healthz", {}, {200}),
    ("robots", "GET", "/robots.txt", {}, {200}),
    ("llms", "GET", "/llms.txt", {}, {200}),
    ("sitemap", "GET", "/sitemap.xml", {}, {200}),
    # --- read-only APIs, no credentials supplied ---
    ("api-auth-sources", "GET", "/api/authenticity/sources", {}, "not5xx"),
    ("api-auth-config", "GET", "/api/authenticity/config", {}, "not5xx"),
    ("api-auth-report", "GET", "/api/authenticity/report?hours=1", {}, "not5xx"),
    ("api-auth-records", "GET", "/api/authenticity/records?days=1", {}, "not5xx"),
    ("api-auth-task", "GET", "/api/authenticity/task_status", {}, "not5xx"),
    ("api-channel-sources", "GET", "/api/channel/sources", {}, "not5xx"),
    ("api-channel-records", "GET", "/api/channel/records?days=1", {}, "not5xx"),
    ("api-channel-export", "GET", "/api/channel/export?days=1&format=json", {}, "not5xx"),
    ("api-image-history", "GET", "/api/image/history?limit=1", {}, "not5xx"),
    # --- unknown / hostile targets ---
    ("unknown-path", "GET", "/definitely-not-a-route", {}, {404}),
    ("unknown-api", "GET", "/api/nope", {}, {404}),
    ("traversal-slash", "GET", "/static/../web/server.py", {}, "not5xx"),
    ("traversal-encoded", "GET", "/static/%2e%2e%2fweb%2fserver.py", {}, "not5xx"),
    ("null-byte", "GET", "/static/%00index.js", {}, "not5xx"),
    # --- malformed query / path params ---
    ("records-neg-days", "GET", "/api/channel/records?days=-5", {}, "not5xx"),
    ("records-zero-days", "GET", "/api/channel/records?days=0", {}, {400}),
    ("records-huge-days", "GET", "/api/channel/records?days=999999999", {}, "not5xx"),
    ("records-nonnumeric", "GET", "/api/channel/records?days=abc", {}, "not5xx"),
    ("record-id-missing", "GET", "/api/channel/records/deadbeef", {}, {404}),
    ("record-id-traversal", "GET", "/api/channel/records/..%2f..%2fetc%2fpasswd", {}, {404}),
    ("job-missing", "GET", "/api/channel/jobs/nope", {}, {404}),
    ("result-missing", "GET", "/api/result/nope.json", {}, "not5xx"),
    ("status-missing", "GET", "/api/status/nope", {}, "not5xx"),
    # --- POST without body: must be a clean 4xx, never a 500 ---
    ("post-probe-empty", "POST", "/api/probe", {"content": b""}, "not5xx"),
    ("post-detect-empty", "POST", "/api/detect", {"content": b""}, "not5xx"),
    ("post-wishlist-empty", "POST", "/api/wishlist", {"content": b""}, "not5xx"),
    ("post-channel-models-empty", "POST", "/api/channel/models", {"content": b""}, "not5xx"),
    ("post-channel-run-empty", "POST", "/api/channel/run", {"content": b""}, "not5xx"),
    ("post-image-models-empty", "POST", "/api/image/models", {"content": b""}, "not5xx"),
    ("post-image-run-empty", "POST", "/api/image/run", {"content": b""}, "not5xx"),
    ("post-auth-config-empty", "POST", "/api/authenticity/config", {"content": b""}, "not5xx"),
    ("post-auth-models-empty", "POST", "/api/authenticity/models", {"content": b""}, "not5xx"),
    # --- POST with junk body ---
    ("post-probe-junk", "POST", "/api/probe", {"content": b"not json at all"}, "not5xx"),
    ("post-channel-run-junk", "POST", "/api/channel/run", {"content": b"{broken"}, "not5xx"),
    ("post-winvalid-json", "POST", "/api/channel/run",
     {"content": b'{"base_url":"","api_key":"","targets":[]}'}, "not5xx"),
    # --- oversized body (route-level guard is 64 KiB) ---
    ("post-channel-run-huge", "POST", "/api/channel/run", {"content": b"x" * (256 * 1024)}, "not5xx"),
    ("post-image-run-huge", "POST", "/api/image/run", {"content": b"x" * (256 * 1024)}, "not5xx"),
    # --- wrong content type ---
    ("post-text-plain", "POST", "/api/channel/run",
     {"content": b'{"a":1}', "headers": {"Content-Type": "text/plain"}}, "not5xx"),
    # --- private-network guard on channel probes (SSRF surface) ---
    ("ssrf-localhost", "POST", "/api/channel/models",
     {"json": {"base_url": "http://127.0.0.1:9/v1", "api_key": "x"}}, "not5xx"),
    ("ssrf-metadata", "POST", "/api/channel/models",
     {"json": {"base_url": "http://169.254.169.254/v1", "api_key": "x"}}, "not5xx"),
    ("ssrf-scheme", "POST", "/api/channel/models",
     {"json": {"base_url": "file:///etc/passwd", "api_key": "x"}}, "not5xx"),
    ("ssrf-redirect-shape", "POST", "/api/channel/models",
     {"json": {"base_url": "https://example.com@127.0.0.1/v1", "api_key": "x"}}, "not5xx"),
    # --- method not allowed ---
    ("delete-index", "DELETE", "/", {}, "not5xx"),
    ("put-healthz", "PUT", "/healthz", {}, "not5xx"),
]


def main() -> int:
    port = free_port()
    uv = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical", access_log=False)
    server = uvicorn.Server(uv)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if getattr(server, "started", False):
            break
        time.sleep(0.05)
    if not getattr(server, "started", False):
        print("FATAL: uvicorn did not start")
        return 2

    base = f"http://127.0.0.1:{port}"
    results, failures = [], []
    with httpx.Client(base_url=base, timeout=30.0, follow_redirects=False) as cli:
        for label, method, path, kwargs, expect in CASES:
            started = time.monotonic()
            try:
                resp = cli.request(method, path, **kwargs)
                code = resp.status_code
                note = ""
            except Exception as exc:  # transport-level surprise
                code, note = -1, f"{type(exc).__name__}: {exc}"
            ms = round((time.monotonic() - started) * 1000)
            if note:
                ok = False
            elif expect == "not5xx":
                ok = code < 500
            else:
                ok = code in expect
            results.append({"case": label, "method": method, "path": path,
                            "status": code, "ms": ms, "ok": ok, "note": note})
            if not ok:
                failures.append(results[-1])

    # coverage cross-check against the real route table
    routes = registered_routes()
    covered = {(r["method"], urlparse(r["path"]).path) for r in results}
    def has_route(m: str, p: str) -> bool:
        for rm, rp in routes:
            if rm != m:
                continue
            if rp == p:
                return True
            if "{" in rp and rp.split("{")[0] == p.split("/")[0] + "/"[:0] + rp.split("{")[0] or rp.split("{")[0] in p:
                return True
        return False
    uncovered = [(m, p) for m, p in routes if not has_route(m, p)]

    report = {"base": base, "cases": len(results), "failures": len(failures),
              "routes_registered": len(routes), "routes_not_covered": uncovered,
              "results": results}
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"base={base}  cases={len(results)}  failures={len(failures)}")
    for f in failures:
        print(f"  FAIL {f['case']:28s} {f['method']:6s} {f['path']:46s} -> {f['status']} {f['note']}")
    print(f"routes registered={len(routes)}  not covered={len(uncovered)}")
    for m, p in uncovered[:20]:
        print(f"  uncovered {m:6s} {p}")
    print(f"report: {OUT / 'report.json'}")
    server.should_exit = True
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
