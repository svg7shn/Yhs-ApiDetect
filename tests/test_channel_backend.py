"""Offline channel contract/security tests. No real upstream requests."""
import asyncio
import csv
import io
import json
import time
from collections import Counter

import httpx
import pytest
from fastapi import FastAPI

from web import channel_jobs as jobs
from web import channel_probe as probe
from web import channel_routes as routes
from web import channel_security as sec
from web import channel_store as store

ORIGINAL_RESOLVER = sec.public_address
KEY = "channel-test-secret-never-persist"
BASE = "https://relay.example/v1"
TARGET = {"model": "gpt-test", "family": "gpt", "claimed_source": "gpt_official"}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("Yhs-ApiDetect_CHANNEL_DIR", str(tmp_path / "channel"))
    jobs.JOBS.clear()
    sec._active_requests = 0
    # Fail closed if any test forgets the network seam.
    async def no_network(*args, **kwargs):
        raise AssertionError("Unexpected DNS/network access")
    monkeypatch.setattr(sec, "public_address", no_network)
    monkeypatch.setattr(routes, "public_address", no_network)


def mock_http(monkeypatch, handler):
    real_client = httpx.AsyncClient
    async def address(url):
        return "93.184.216.34"
    monkeypatch.setattr(sec, "public_address", address)
    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return real_client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(sec.httpx, "AsyncClient", factory)


@pytest.mark.parametrize("url", ["ftp://example.org", "https://a:b@example.org", "https://example.org?", "https://example.org#", "https://example.org/a/../b", "https://example.org/%2e", "https://localhost", "https://example.org\\x", "https://example.org:99999", " https://example.org"])
def test_bad_url(url):
    with pytest.raises(sec.ChannelError):
        sec.normalize_url(url)


@pytest.mark.parametrize("status,code", [(401,"unauthorized"),(403,"forbidden"),(429,"rate_limited"),(404,"not_found"),(302,"redirect_blocked"),(500,"upstream_error")])
async def test_status_no_echo(monkeypatch, status, code):
    mock_http(monkeypatch, lambda req: httpx.Response(status, json={"message": KEY}, headers={"location": "http://127.0.0.1"}))
    with pytest.raises(sec.ChannelError) as caught:
        await sec.request_json("GET", BASE + "/models", KEY)
    assert caught.value.code == code
    assert KEY not in str(caught.value.payload())
    assert sec._active_requests == 0


async def test_pinned_host_and_sni(monkeypatch):
    def handler(req):
        assert req.url.host == "93.184.216.34"
        assert req.headers["host"] == "relay.example"
        assert req.extensions["sni_hostname"] == "relay.example"
        assert req.headers["authorization"] == "Bearer " + KEY
        return httpx.Response(200, json={"data": []})
    mock_http(monkeypatch, handler)
    assert await sec.request_json("GET", BASE + "/models", KEY) == {"data": []}


@pytest.mark.parametrize("payload,limit,code", [(b"no json",1024,"invalid_response"),(b"x"*100,10,"response_too_large")])
async def test_bad_response(monkeypatch, payload, limit, code):
    monkeypatch.setattr(sec, "MAX_RESPONSE_BYTES", limit)
    mock_http(monkeypatch, lambda r: httpx.Response(200, content=payload))
    with pytest.raises(sec.ChannelError) as caught:
        await sec.request_json("GET", BASE, KEY)
    assert caught.value.code == code


async def test_ssrf_dns(monkeypatch):
    # Original resolver, but only mocked getaddrinfo (including mixed-answer rebinding).
    original = ORIGINAL_RESOLVER
    async def resolve(*a, **k):
        return [(2,1,6,"",("93.184.216.34",443)), (2,1,6,"",("127.0.0.1",443))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(sec.ChannelError, match="不允许"):
        await original(BASE)


async def test_models_pagination(monkeypatch):
    calls = []
    async def request(method, url, key, **kwargs):
        calls.append(kwargs.get("params"))
        return {"data": [{"id":"gpt-test"}], "has_more": True, "last_id": "gpt-test"} if len(calls)==1 else {"data":[{"id":"claude-test"},{"id":"gemini-test"},{"id":KEY}]}
    monkeypatch.setattr(probe, "request_json", request)
    result = await probe.list_models(BASE, KEY)
    assert [x["family"] for x in result["models"]] == ["gpt", "claude", "gemini"]
    assert calls == [None, {"after_id":"gpt-test"}]
    assert KEY not in json.dumps(result)


async def test_models_404_not_success(monkeypatch):
    async def request(*a, **k):
        raise probe.ChannelError("not_found", "404", 404)
    monkeypatch.setattr(probe, "request_json", request)
    with pytest.raises(probe.ChannelError) as caught:
        await probe.list_models(BASE, KEY)
    assert caught.value.status == 404


async def test_models_loop(monkeypatch):
    async def request(*a, **k):
        return {"models": [], "nextPageToken": "same"}
    monkeypatch.setattr(probe, "request_json", request)
    with pytest.raises(probe.ChannelError) as caught:
        await probe.list_models(BASE, KEY)
    assert caught.value.code == "invalid_pagination"


def good_payload(family, text):
    if family == "gpt":
        return {"id":"chatcmpl-x", "object":"chat.completion", "model":"gpt-test", "choices":[{"message":{"content":text}}], "usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}, "signature": KEY}
    if family == "claude":
        return {"id":"msg_x", "type":"message", "model":"claude-test", "content":[{"type":"text","text":text}], "usage":{"input_tokens":1,"output_tokens":1,"cache_read_input_tokens":0}}
    return {"modelVersion":"gemini-test", "candidates":[{"content":{"parts":[{"text":text}]}}], "usageMetadata":{"promptTokenCount":1,"candidatesTokenCount":1,"totalTokenCount":2}}


@pytest.mark.parametrize("family", ["claude","gpt","gemini"])
async def test_probe_conservative_no_raw_secrets(monkeypatch, family):
    monkeypatch.setattr(probe, "challenge", lambda: ("question", [6,7,8]))
    calls=[]
    async def request(*a, **k):
        calls.append(k["body"])
        return good_payload(family, f"ANSWER1: 6\nANSWER2: 7\nANSWER3: 8\nIDENTITY: {family} {KEY}")
    monkeypatch.setattr(probe, "request_json", request)
    target={"model":family+"-test", "family":family, "claimed_source":family+"_official", "wire_family":family}
    record=await probe.probe_target(BASE, KEY, target, 30)
    # 全部有效检查通过且达到 authentic 阈值 → 判为与声称来源一致。
    assert record["verdict"] == "authentic"
    assert record["score"] == 100
    assert record["capability_accuracy"] == 1.0
    assert len(calls)==2
    assert KEY not in json.dumps(record)
    assert all(e["status"] == "unknown" for e in record["evidence"] if e["name"] in {"签名字段", "来源证明", "缓存字段"})


async def test_jobs_continue_and_store(monkeypatch, tmp_path):
    async def run(base,key,target,timeout):
        if target["model"] == "gpt-bad":
            raise RuntimeError(KEY)
        return probe.record_for(base, target, [probe.evidence("协议", "pass", "OK")], time.monotonic())
    monkeypatch.setattr(jobs,"probe_target",run)
    job_id=jobs.start(BASE, KEY, [dict(TARGET,model="gpt-bad"), TARGET], 1)
    await asyncio.gather(*jobs.TASKS)
    job=jobs.get(job_id)
    assert job["status"]=="completed" and job["completed"]==2
    assert [r["verdict"] for r in job["records"]] == ["error","inconclusive"]
    assert len(store.query())==2
    assert KEY not in json.dumps(job)
    assert KEY.encode() not in (tmp_path/"channel"/"records.sqlite3").read_bytes()
    assert not jobs.TASKS


@pytest.mark.parametrize("family,wire", [("gpt", "gpt"), ("claude", "gpt"), ("gemini", "gpt"), ("claude", "claude"), ("gemini", "gemini")])
async def test_wire_url_body_and_identity(monkeypatch, family, wire):
    monkeypatch.setattr(probe, "challenge", lambda: ("question", [6, 7, 8]))
    calls = []
    async def request(method, url, key, **kwargs):
        calls.append((url, kwargs["body"]))
        payload = good_payload(wire, f"ANSWER1: 6\nANSWER2: 7\nANSWER3: 8\nIDENTITY: {family}")
        payload["modelVersion" if wire == "gemini" else "model"] = family + "-test"
        return payload
    monkeypatch.setattr(probe, "request_json", request)
    target = dict(model=family+"-test", family=family, claimed_source=family+"_official")
    if wire != "gpt":
        target["wire_family"] = wire
    record = await probe.probe_target(BASE, KEY, target, 1)
    expected_url = BASE + ("/messages" if wire == "claude" else "/chat/completions")
    if wire == "gemini":
        expected_url = "https://relay.example/v1beta/models/gemini-test:generateContent"
    assert len(calls) == 2 and all(url == expected_url for url, body in calls)
    expected_body = {"contents": [{"role": "user", "parts": [{"text": "question"}]}], "generationConfig": {"maxOutputTokens": 512}} if wire == "gemini" else {"model": target["model"], "messages": [{"role": "user", "content": "question"}], "max_tokens" if wire == "claude" else "max_completion_tokens": 512}
    assert calls[0][1] == expected_body
    if wire == "gemini":
        assert calls[0][1]["generationConfig"]["maxOutputTokens"] == 512
        assert calls[1][1]["generationConfig"]["maxOutputTokens"] == "invalid-channel-probe"
    else:
        budget = "max_tokens" if wire == "claude" else "max_completion_tokens"
        assert calls[0][1][budget] == 512
        assert calls[1][1][budget] == "invalid-channel-probe"
    assert record["verdict"] == "authentic" and record["score"] == 100
    assert all(e["status"] == "pass" for e in record["evidence"] if e["name"] in ("自报身份", "回显模型"))


@pytest.mark.parametrize("wire,payload", [
    ("gpt", {"choices": [{"finish_reason": reason}]}) for reason in ("length", "content_filter", "refusal")
] + [
    ("gpt", {"choices": [{"message": {"refusal": "blocked"}}]}),
    ("claude", {"stop_reason": "max_tokens"}),
    ("claude", {"stop_reason": "refusal"}),
    ("gemini", {"candidates": [{"finishReason": "MAX_TOKENS"}]}),
    ("gemini", {"candidates": [{"finishReason": "SAFETY"}]}),
    ("gemini", {"promptFeedback": {"blockReason": "SAFETY"}}),
    ("gemini", {"promptFeedback": {"safetyRatings": [{"blocked": True}]}}),
])
async def test_incomplete_is_unknown_not_suspect(monkeypatch, wire, payload):
    ev = probe.inspect_payload(payload, wire, [6, 7, 8], wire)
    assert ev and all(e["status"] == "unknown" for e in ev)
    calls = []
    async def request(*args, **kwargs):
        calls.append(kwargs)
        return payload
    monkeypatch.setattr(probe, "request_json", request)
    target = dict(model=wire+"-test", family=wire, claimed_source=wire+"_official", wire_family=wire)
    record = await probe.probe_target(BASE, KEY, target, 1)
    assert record["verdict"] == "inconclusive" and record["score"] is None
    assert len(calls) == 2
    assert all(e["status"] == "unknown" for e in record["evidence"])


async def test_probe_never_retries_paid_failure(monkeypatch):
    calls = []
    async def request(*args, **kwargs):
        calls.append(args)
        raise probe.ChannelError("not_found", "404", 404)
    monkeypatch.setattr(probe, "request_json", request)
    result = await probe.probe_target(BASE, KEY, TARGET, 1)
    assert result["verdict"] == "error" and len(calls) == 1


async def test_wire_route_boundaries_and_thirteen_targets(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    seen = []
    async def safe(base): return "93.184.216.34"
    async def detect(base, key, target, timeout):
        seen.append(target.copy())
        return probe.record_for(base, target, [probe.evidence("协议", "pass", "OK")], time.monotonic())
    monkeypatch.setattr(routes, "public_address", safe)
    monkeypatch.setattr(jobs, "probe_target", detect)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for wire in (None, "", "openai", "GPT", 1, True, [], {}, "claude", "gemini"):
            response = await client.post("/api/channel/run", json={"base_url": BASE, "api_key": KEY, "targets": [dict(TARGET, wire_family=wire)]})
            assert response.status_code == 400 and "invalid_wire_family" in response.text
        assert not seen
        targets = []
        for i in range(13):
            family = ("gpt", "claude", "gemini")[i % 3]
            target = dict(model=family+"-"+str(i), family=family, claimed_source=family+"_official")
            if i >= 3:
                target["wire_family"] = family if i % 2 else "gpt"
            targets.append(target)
        response = await client.post("/api/channel/run", json={"base_url": BASE, "api_key": KEY, "targets": targets})
        assert response.status_code == 200
        await asyncio.gather(*jobs.TASKS)
        assert seen == [dict(t, wire_family=t.get("wire_family", "gpt")) for t in targets]
        assert jobs.get(response.json()["job_id"])["completed"] == 13


def test_source_counts():
    assert Counter(s["family"] for s in probe.SOURCES)=={"claude":7,"gpt":2,"gemini":2}


def test_store_export_and_filters():
    record=probe.record_for(BASE,TARGET,[probe.evidence("协议","pass","<script>alert(1)</script>")],time.monotonic())
    record["summary"]="=HYPERLINK(1)"
    store.save(record)
    assert store.get(record["id"])==record
    assert store.get("../../secret") is None
    assert len(store.query(7,"relay"))==1 and store.query(7,"absent")==[]
    csv_body,_=store.export([record],"csv")
    rows=list(csv.reader(io.StringIO(csv_body)))
    assert rows[1][-1].startswith("'=")
    md,_=store.export([record],"md")
    assert "<script>" not in md
    assert json.loads(store.export([record],"json")[0])["records"][0]["id"]==record["id"]


async def test_api_contract_and_validation(monkeypatch):
    app=FastAPI(); app.include_router(routes.router)
    async def safe(base): return "93.184.216.34"
    async def models(base,key): return {"ok":True,"base_url":base,"models":[{"id":"gpt-test","family":"gpt"}],"endpoint":base+"/models"}
    async def detect(base,key,target,timeout): return probe.record_for(base,target,[probe.evidence("协议","pass","OK")],time.monotonic())
    monkeypatch.setattr(routes,"public_address",safe)
    monkeypatch.setattr(routes,"list_models",models)
    monkeypatch.setattr(jobs,"probe_target",detect)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        assert len((await client.get("/api/channel/sources")).json()["sources"])==11
        bad=await client.post("/api/channel/run",json={"base_url":BASE,"api_key":KEY,"targets":[dict(TARGET,claimed_source="claude_official")]})
        assert bad.status_code==400 and KEY not in bad.text
        res=await client.post("/api/channel/models",json={"base_url":BASE,"api_key":KEY})
        assert res.json()["ok"]
        created=await client.post("/api/channel/run",json={"base_url":BASE,"api_key":KEY,"targets":[TARGET],"timeout":30})
        await asyncio.gather(*jobs.TASKS)
        job=(await client.get("/api/channel/jobs/"+created.json()["job_id"])).json()
        assert job["status"]=="completed"
        record=(await client.get("/api/channel/records/"+job["records"][0]["id"])).json()
        assert "id" in record and "record" not in record
        assert len((await client.get("/api/channel/records?days=7&q=relay")).json()["records"])==1
        for fmt in ("csv","json","md"):
            response=await client.get("/api/channel/export?format="+fmt)
            assert response.status_code==200 and KEY not in response.text
        assert (await client.get("/api/channel/records?days=0")).status_code==400
        assert (await client.get("/api/channel/jobs/missing")).status_code==404
        malformed=await client.post("/api/channel/run",content=KEY)
        assert malformed.status_code==400 and KEY not in malformed.text


@pytest.mark.parametrize("kind,code", [("timeout", "timeout"), ("network", "unreachable")])
async def test_transport_failures(monkeypatch, kind, code):
    def handler(request):
        if kind == "timeout":
            raise httpx.ReadTimeout(KEY)
        raise httpx.ConnectError(KEY)
    mock_http(monkeypatch, handler)
    with pytest.raises(sec.ChannelError) as caught:
        await sec.request_json("GET", BASE, KEY)
    assert caught.value.code == code and KEY not in str(caught.value)


async def test_request_limit(monkeypatch):
    sec._active_requests = sec.MAX_ACTIVE_REQUESTS
    with pytest.raises(sec.ChannelError) as caught:
        await sec.request_json("GET", BASE, KEY)
    assert caught.value.code == "busy"


async def test_job_capacity_eviction_and_release(monkeypatch):
    monkeypatch.setattr(jobs, "MAX_RUNNING", 1)
    monkeypatch.setattr(jobs, "MAX_JOBS", 1)
    entered, release = asyncio.Event(), asyncio.Event()
    async def detect(base, key, target, timeout):
        entered.set()
        await release.wait()
        return probe.record_for(base, target, [probe.evidence("协议", "pass", "OK")], time.monotonic())
    monkeypatch.setattr(jobs, "probe_target", detect)
    first = jobs.start(BASE, KEY, [TARGET], 1)
    await entered.wait()
    with pytest.raises(jobs.ChannelError):
        jobs.start(BASE, KEY, [TARGET], 1)
    release.set()
    tasks = list(jobs.TASKS)
    await asyncio.gather(*tasks)
    assert tasks[0].get_coro().cr_frame is None
    second = jobs.start(BASE, KEY, [TARGET], 1)
    await asyncio.gather(*jobs.TASKS)
    assert first not in jobs.JOBS and second in jobs.JOBS


async def test_job_cancel_releases_key(monkeypatch):
    entered = asyncio.Event()
    async def detect(*args):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(jobs, "probe_target", detect)
    job_id = jobs.start(BASE, KEY, [TARGET], 1)
    await entered.wait()
    task = next(iter(jobs.TASKS))
    task.cancel()
    await task
    assert jobs.get(job_id)["status"] == "failed"
    assert task.get_coro().cr_frame is None


async def test_models_page_limit(monkeypatch):
    count = 0
    async def request(*a, **k):
        nonlocal count
        count += 1
        return {"models": [], "nextPageToken": str(count)}
    monkeypatch.setattr(probe, "request_json", request)
    with pytest.raises(probe.ChannelError) as caught:
        await probe.list_models(BASE, KEY)
    assert caught.value.code == "page_limit" and count == probe.MAX_PAGES


async def test_route_error_status_and_target_limit(monkeypatch):
    app = FastAPI(); app.include_router(routes.router)
    async def failed(*a):
        raise routes.ChannelError("forbidden", "上游禁止访问（403）", 403)
    monkeypatch.setattr(routes, "list_models", failed)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/channel/models", json={"base_url": BASE, "api_key": KEY})
        assert response.status_code == 403 and response.json() == {"ok": False, "error": {"code": "forbidden", "message": "上游禁止访问（403）"}}
        for targets in ([], [TARGET] * (routes.MAX_TARGETS + 1)):
            response = await client.post("/api/channel/run", json={"base_url": BASE, "api_key": KEY, "targets": targets})
            assert response.status_code == 400
        response = await client.post("/api/channel/run", content="x" * (routes.MAX_BODY + 1))
        assert response.status_code == 413


def test_effective_score_and_subscription_warning():
    ev = [probe.evidence("a", "pass", ""), probe.evidence("b", "pass", ""), probe.evidence("c", "fail", ""),
          probe.evidence("d", "unknown", ""), probe.evidence("e", "error", "")]
    record = probe.record_for(BASE, TARGET, ev, time.monotonic())
    # 3 项有效检查（2 通过 1 未通过）→ 66.7 分，落在 suspect 区间。
    assert record["score"] == round(100 * 2 / 3, 1) and record["verdict"] == "suspect"


def test_verdict_requires_min_scored_signals():
    """有效检查不足 3 项时不给结论，回落 inconclusive。"""
    few = [probe.evidence("a", "pass", ""), probe.evidence("b", "fail", "")]
    assert probe.record_for(BASE, TARGET, few, time.monotonic())["verdict"] == "inconclusive"
    none = [probe.evidence("a", "unknown", ""), probe.evidence("b", "unknown", "")]
    assert probe.record_for(BASE, TARGET, none, time.monotonic())["verdict"] == "inconclusive"


def test_unprovable_source_never_authentic():
    """订阅 / 合作伙伴来源无法通过本 API 证明，即使满分也不判 authentic。"""
    ev = [probe.evidence("a", "pass", ""), probe.evidence("b", "pass", ""), probe.evidence("c", "pass", "")]
    for source in ("claude_ccmax", "claude_q_kiro", "claude_aws_partner"):
        target = dict(TARGET, claimed_source=source)
        assert probe.record_for(BASE, target, ev, time.monotonic())["verdict"] == "inconclusive"


def test_low_score_fails():
    """加权分低于 suspect 阈值判 fail。"""
    ev = [probe.evidence("a", "fail", ""), probe.evidence("b", "fail", ""), probe.evidence("c", "pass", ""), probe.evidence("d", "fail", "")]
    record = probe.record_for(BASE, TARGET, ev, time.monotonic())
    assert record["score"] == 25.0 and record["verdict"] == "fail"


def test_info_signals_do_not_score():
    """weight=0 的信息性信号不参与加权，也不占有效项。"""
    ev = [probe.evidence("a", "pass", ""), probe.evidence("b", "pass", ""), probe.evidence("c", "pass", ""),
          probe.evidence("g", "unknown", "", "gateway", 0.0, "gateway_software")]
    record = probe.record_for(BASE, TARGET, ev, time.monotonic())
    assert record["score"] == 100 and record["verdict"] == "authentic"


def test_claude_source_provenance_prefixes():
    """按消息 ID 前缀核对声称来源：跨云前缀判 fail。"""
    assert probe.claude_source_signal({"id": "msg_bdrk_abc"}, "claude_bedrock")["status"] == "pass"
    assert probe.claude_source_signal({"id": "msg_vrtx_abc"}, "claude_bedrock")["status"] == "fail"
    assert probe.claude_source_signal({"id": "msg_vrtx_abc"}, "claude_vertex")["status"] == "pass"
    assert probe.claude_source_signal({"id": "msg_bdrk_abc"}, "claude_vertex")["status"] == "fail"
    assert probe.claude_source_signal({"id": "msg_bdrk_abc"}, "claude_official")["status"] == "fail"
    assert probe.claude_source_signal({"id": "msg_01abc"}, "claude_official")["status"] == "unknown"
    assert probe.claude_source_signal({"id": "msg_bdrk_abc"}, "claude_bedrock")["weight"] == 1.5
    assert probe.claude_source_signal({"id": "msg_vrtx_abc"}, "claude_bedrock")["weight"] == 4.0


async def test_ccmax_not_proven(monkeypatch):
    async def request(*a, **k):
        return good_payload("claude", "IDENTITY: Claude")
    monkeypatch.setattr(probe, "request_json", request)
    record = await probe.probe_target(BASE, KEY, {"model": "claude-test", "family": "claude", "claimed_source": "claude_ccmax"}, 1)
    assert any("订阅/合作伙伴来源无法" in e["detail"] for e in record["evidence"])
