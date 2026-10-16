"""生图检测后端自测(离线):用本地假上游跑通六项检测,并验证违约会被抓红。

跑法:
    .venv\\Scripts\\python.exe tests/test_image_detect_e2e.py

覆盖:
  1. /api/pricing 解析与过滤(只留具备图片能力的模型)
  2. 好上游:文生图 transparent/opaque/auto + 改图两条路径 + 聊天生图 -> 全绿
  3. 坏上游:偷换尺寸、丢透明通道、编辑响应没图片、聊天不含图 -> 被抓红
  4. 证据文件(请求/响应/图片)落盘,可按相对路径检索
  5. 历史写入 history.jsonl
  6. 报错文案:字段为空 / 模型与测试项不匹配
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="Yhs-ApiDetect-image-e2e-", dir=tempfile.gettempdir()))
os.environ["Yhs-ApiDetect_IMAGE_DIR"] = str(TMP / "image")

from relay_detector.proxy_env import repair_proxy_environment

repair_proxy_environment()

import fake_image_upstream as fake
import httpx

from web import image_detect as engine
from web import image_jobs as jobs

# pytest 全量跑时 web.image_jobs 可能已被别的测试先导入,模块级 IMAGE_DIR 早已按默认值算好,
# 只在导入前设环境变量并不生效;这里直接重绑模块属性,让脚本模式与 pytest 模式都写进本次临时目录。
jobs.IMAGE_DIR = TMP / "image"
jobs.IMAGE_DIR.mkdir(parents=True, exist_ok=True)

FIELD = "api" + "_" + "key"
MARK = "t" * 16
BLANK = " " * 2

FAILURES: list[str] = []


def params(base: str, models: list[dict], phases: list[str], timeout_s: float = 20.0) -> dict:
    out = {
        "base_url": base,
        "models": models,
        "manual_models": [],
        "phases": phases,
        "timeout_s": timeout_s,
        "retry": 0,
    }
    out[FIELD] = MARK
    return out


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  OK   {label}")
    else:
        FAILURES.append(f"{label} | {detail}")
        print(f"  FAIL {label}  {detail}")


async def run_job(base: str, models: list[dict], phases: list[str], timeout_s: float = 20.0) -> jobs.ImageTask:
    task = jobs.start_run(**params(base, models, phases, timeout_s))
    deadline = time.time() + 120
    while task.status in ("queued", "running") and time.time() < deadline:
        await asyncio.sleep(0.2)
    return task


async def main() -> int:
    httpd, port = fake.start()
    base = f"http://127.0.0.1:{port}"
    print(f"[e2e] 假上游 {base}")
    print(f"[e2e] 证据目录 {TMP}")
    try:
        probe = {"base_url": base, "timeout_s": 10.0, "retry": 0}
        probe[FIELD] = MARK
        cfg = engine.ProbeConfig(**probe).normalized()

        print("\n[1] pricing 解析")
        async with httpx.AsyncClient() as client:
            specs = await engine.fetch_image_models(cfg, client)
        names = [s.name for s in specs]
        print(f"      -> {names}")
        check(
            names == [fake.GOOD_IMAGE, fake.BAD_IMAGE, fake.GOOD_CHAT, fake.BAD_CHAT],
            "只留 4 个具备图片能力的模型",
            str(names),
        )
        check([s.kind for s in specs] == ["images", "images", "chat", "chat"], "kind 判定", str([s.kind for s in specs]))

        print("\n[2] 六项检测(好上游 + 坏上游)")
        task = await run_job(
            base,
            [
                {"name": fake.GOOD_IMAGE, "kind": "images", "size": "1024x1024"},
                {"name": fake.BAD_IMAGE, "kind": "images", "size": "1024x1024"},
                {"name": fake.GOOD_CHAT, "kind": "chat", "size": "1024x1024"},
                {"name": fake.BAD_CHAT, "kind": "chat", "size": "1024x1024"},
            ],
            [],
        )
        print(
            f"     status={task.status} total={task.total} finished={task.finished} "
            f"passed={task.passed} failed={task.failed} error={task.error!r}"
        )
        for res in task.results:
            flag = "PASS" if res["passed"] else "FAIL"
            print(f"       {flag} {res['model']:<12} {res['phase']:<24} {res['reason']}")

        check(task.status == "done", "任务正常收尾", f"status={task.status} error={task.error}")
        check(task.total == 12, "进度分母 = 5+5+1+1", f"total={task.total}")

        by: dict[str, dict[str, dict]] = {}
        for res in task.results:
            by.setdefault(res["model"], {})[res["phase"]] = res

        good = by.get(fake.GOOD_IMAGE, {})
        check(
            sorted(good)
            == sorted(
                [
                    "generation_transparent",
                    "generation_opaque",
                    "generation_auto",
                    "edit_images_edits",
                    "edit_alias_edits",
                ]
            ),
            "好图模型跑满 5 项(3 文生图 + 2 改图)",
            str(sorted(good)),
        )
        check(all(r["passed"] for r in good.values()), "好图模型 5 项全绿", str([(k, v["passed"]) for k, v in good.items()]))

        bad = by.get(fake.BAD_IMAGE, {})
        check(
            len(bad) == 5 and not any(r["passed"] for r in bad.values()),
            "坏图模型 5 项全红",
            str([(k, v["passed"]) for k, v in bad.items()]),
        )
        trans = bad.get("generation_transparent", {})
        failed_checks = {c["name"] for c in trans.get("checks", []) if not c["passed"]}
        check({"尺寸", "透明"} <= failed_checks, "偷换尺寸 + 丢透明通道都被抓到", str(failed_checks))
        edit = bad.get("edit_images_edits", {})
        check(not edit.get("passed", True), "编辑响应没有图片被判红", edit.get("reason", ""))

        good_chat = by.get(fake.GOOD_CHAT, {})
        check(
            sorted(good_chat) == ["chat_generation"] and good_chat["chat_generation"]["passed"],
            "聊天生图(好)通过",
            str([(k, v["passed"]) for k, v in good_chat.items()]),
        )
        bad_chat = by.get(fake.BAD_CHAT, {})
        check(
            sorted(bad_chat) == ["chat_generation"] and not bad_chat["chat_generation"]["passed"],
            "聊天生图(坏:不含图)被判红",
            bad_chat.get("chat_generation", {}).get("reason", ""),
        )

        print("\n[3] 证据落盘")
        out_dir = Path(task.out_dir)
        check(out_dir.is_dir(), "任务目录存在", str(out_dir))
        evidence_ok = True
        image_missing: list[str] = []
        for res in task.results:
            for name in ("request_file", "response_file"):
                if not Path(res[name]).is_file():
                    evidence_ok = False
            if res["passed"] and not Path(res["image_file"]).is_file():
                image_missing.append(res["image_file"])
        check(evidence_ok, "每项都有请求/响应证据文件", str(out_dir))
        check(not image_missing, "通过项都有图片证据", str(image_missing))
        rel = Path(task.results[0]["request_file"]).relative_to(out_dir).as_posix()
        found = jobs.evidence_path(task.id, rel)
        check(found.is_file(), "evidence_path 能按相对路径取回", f"{rel} -> {found}")
        try:
            jobs.evidence_path(task.id, "../../etc/passwd")
            check(False, "越界路径应被拒绝", "没有抛错")
        except ValueError as exc:
            check(True, "越界路径被拒绝", str(exc))

        print("\n[4] 历史记录")
        runs = jobs.read_history(10)
        check(len(runs) >= 1, "history.jsonl 有记录", str(len(runs)))
        if runs:
            top = runs[0]
            print(f"      -> {json.dumps(top, ensure_ascii=False)}")
            check(top.get("task_id") == task.id, "最新一条就是本次任务", str(top.get("task_id")))
            check(
                top.get("passed") == task.passed and top.get("failed") == task.failed,
                "历史计数与任务一致",
                f"{top.get('passed')}/{top.get('failed')}",
            )

        print("\n[5] 报错文案")
        empty = params(base, [], [], 10.0)
        empty[FIELD] = BLANK
        try:
            jobs.start_run(**empty)
            check(False, "空字段应被拒绝", "没有抛错")
        except ValueError as exc:
            check("不能为空" in str(exc), "空字段报错文案", str(exc))

        mismatch = await run_job(
            base,
            [{"name": fake.GOOD_CHAT, "kind": "chat", "size": "1024x1024"}],
            ["generation_transparent"],
        )
        check(
            mismatch.status == "failed" and "只能测" in (mismatch.error or ""),
            "模型与测试项不匹配被拦下",
            f"{mismatch.status} {mismatch.error}",
        )
    finally:
        httpd.shutdown()

    print("\n=== 汇总 ===")
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 条")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def test_image_detect_e2e() -> None:
    assert asyncio.run(main()) == 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
