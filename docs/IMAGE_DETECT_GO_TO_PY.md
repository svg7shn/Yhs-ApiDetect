# 生图检测：Go 判据 → Python 实现 对照表

本文件记录 Yhs-ApiDetect「生图检测」从外部 Go 服务改为站内 Python 自实现时，逐条对齐的行为判据。
Go 源码仅作行为说明书，Python 实现为原创代码。

- Go 源（参考物，不再被 Yhs-ApiDetect 依赖）：`E:\Working\image\image-param-verifier-main\main.go`
- Python 实现：`web/image_detect.py`（检测引擎）、`web/image_jobs.py`（任务运行器）
- 前端：`web/templates/image.html`、`web/static/image.css`、`web/static/image.js`
- 站内接线：`web/server.py` 的 `GET /image` 与 `/api/image/*`

## 1. 测试项与常量

| Go（main.go） | Python | 对齐点 |
| --- | --- | --- |
| `allPhaseInfos`（:451-458） | `PHASE_INFO` | 六项顺序与展示名逐字沿用：文生图 · transparent / opaque / auto、改图 · /v1/images/edits、改图 · /v1/edits、聊天生图 · /v1/chat/completions |
| `phaseChatGeneration` 等（:429-441） | `PHASE_CHAT` / `PHASE_GEN_*` / `PHASE_EDIT_*` / `PHASE_EDIT_SOURCE` | 取值沿用 Go 内部名：chat_generation、generation_transparent、generation_opaque、generation_auto、edit_images_edits、edit_alias_edits、edit_source |
| `normalizePhases`（:474） | `normalize_phases` | 未知项报「未知的测试项: X」；空集合＝全部测试项 |
| `hasApplicablePhase`（:532） | `has_applicable_phase` | chat 类模型只认聊天生图，images 类只认 5 个文生图/改图项 |
| `phaseSummary` / `buildSelectionLabel`（:523、:544） | `ImageTask._selection_label` | 历史记录里的「N 个模型 | 测试项摘要」文案 |

## 2. 模型发现

| Go | Python | 对齐点 |
| --- | --- | --- |
| `fetchImageModels`（:92-130） | `fetch_image_models` | `GET {base}/api/pricing`，不带鉴权头，30 秒超时；错误三态文案「请求 pricing 接口失败: …」「pricing 接口 HTTP N」「解析 pricing JSON 失败: …」 |
| `isImageCapableModel`（:133-159） | `is_image_capable_model` | 三条件：supported_endpoint_types 含 image-generation；image_output_price > 0；名称含关键词且不含 video |
| `detectKind`（:170-175） | `detect_kind` | 含 image-generation → `images`，否则 `chat` |
| `detectSize`（:177-189） | `detect_size` | 含 4k → 4096x4096；含 2k → 2048x2048；其余 → 1024x1024 |
| `resolveModels` / `selectModels`（:662-708） | `resolve_models` / `normalize_specs` | 勾选模型 > 手填模型名 > 自动拉全部；都空时报「接口未返回具备图片能力的模型(可手动填写模型名)」 |

## 3. 请求与响应解码

| Go | Python | 对齐点 |
| --- | --- | --- |
| `postJSON`（:1272-1291） | `_post_json` | 头 `Authorization: Bearer …` + `Content-Type: application/json`，超时取配置值 |
| `postMultipart`（:1293-1348） | `_post_multipart` | 文件 part 名固定 `image`，其余为普通字段 model/prompt/n/size/background/output_format |
| `decodeImageFromResponse`（:1350-1370） | `decode_image_from_response` | data 为空 →「响应 data 为空」；无 b64_json 且无 url →「响应中既没有 b64_json 也没有 url」 |
| `decodeImageFromChatResponse`（:1160-1266）、`extractImageFromContent`（:1199）、`extractImageFromString`（:1229）、`decodeImageURL`（:1244） | `decode_image_from_chat`、`_extract_image_from_content`、`_extract_image_from_string`、`_decode_image_url` | 依次尝试 data[]、choices[0].message.images[].image_url.url、content；data URL 与 markdown 图片两套正则同构；下载失败文案「下载图片失败，HTTP N」 |
| `saveImage`（:1372-1379） | `save_png` | 无论上游返回什么格式，一律编码为 PNG 落盘 |

## 4. 判据（核心）

| Go | Python | 对齐点 |
| --- | --- | --- |
| `checkImage`（:1395-1476） | `check_image` | 三项检查名依次为「尺寸」「格式」「透明」；尺寸文案「期望 WxH，实际 WxH」、透明文案「请求 X，实际存在透明像素=真/假」、auto 恒通过 |
| `parseSize`（:1478-1491） | `parse_size` | 小写后按 x 拆两段整数，否则「无效尺寸: X」 |
| `collectFailReasons`（:1385-1393） | `collect_fail_reasons` | 失败项按「检查名：说明」用「；」连接，整体写入 reason |
| `doGeneration`（:909-985） | `do_generation` | 检查项序列：保存请求 → HTTP 请求 → 保存响应 → HTTP 状态码 → 解析响应 JSON → 接口错误字段 → 解码图片 → 保存图片 → 尺寸/格式/透明 |
| `doEdit`（:987-1077） | `do_edit` | 同序列，多一步保存原图；两条编辑路径 /v1/images/edits 与 /v1/edits |
| `doChatGeneration`（:1079-1158） | `do_chat_generation` | 解码图片一项在聊天链路里显示为「提取图片」 |
| `runWithRetry`（:861-890） | `_with_retry` | 尝试次数 = retry + 1，通过即返回 |
| `testModel`（:710-859） | `test_model` | 每个模型逐项调用；只勾改图时自动补跑一张原图（结果为 edit_source，理由「自动生成原图(仅勾选改图测试,作为编辑输入)」）；没有原图则判「前置条件」失败 |

证据文件名与 Go 一致：`generation_<bg>_request.json` / `generation_<bg>_response.json` / `original_<bg>.png` / `<phase>_request.json` / `<phase>_response.json` / `edited_<phase>.png` / `chat_generation_*.json` / `chat_generated.png` / `prompt.txt`。

## 5. 三处刻意差异

1. **无账号体系**：Go 版用本地账号（bcrypt 用户表 + sessions 表）保护除首页与 /api/auth/* 外的全部接口；Yhs-ApiDetect 版是本地直跑工具，`/api/image/*` 不需要登录。
2. **无 SSE**：Go 版用 `GET /api/jobs/{id}/events` 推流；Yhs-ApiDetect 版沿用站内既有范式（建任务 + 轮询 `GET /api/image/task_status`），全站不引 SSE。
3. **前端原创重做**：Go 版控制台是内嵌在二进制里的页面；Yhs-ApiDetect 版是独立模板与静态资源，不复制其 UI。

## 6. 本地怎么跑

Windows 上必须先把数据目录从默认的 Linux 路径挪出来（默认值 `/opt/Yhs-ApiDetect/...` 在 Windows 会被解析成当前盘根，导致建目录失败）：

```powershell
cd E:\dsh\Yhs-ApiDetect
$env:PYTHONPATH = "E:\dsh\Yhs-ApiDetect\src"
$env:Yhs-ApiDetect_JOBS_DIR = "E:\dsh\Yhs-ApiDetect\web_data\jobs"
$env:Yhs-ApiDetect_IMAGE_DIR = "E:\dsh\Yhs-ApiDetect\web_data\image"
$env:Yhs-ApiDetect_AUTHENTICITY_DIR = "E:\dsh\Yhs-ApiDetect\web_data\authenticity"
$env:Yhs-ApiDetect_AUTHENTICITY_CONFIG = "E:\dsh\Yhs-ApiDetect\web_data\authenticity_channels.json"
$env:Yhs-ApiDetect_WISHLIST_PATH = "E:\dsh\Yhs-ApiDetect\web_data\wishlist.txt"
.venv\Scripts\python.exe -m uvicorn web.server:app --host 127.0.0.1 --port 8123
```

打开 <http://127.0.0.1:8123/image>：填中转站地址与 Key → 「读取模型列表」或「手填模型名」→ 勾测试项 → 「开始检测」。

离线自测（不开真实中转站，用内置假上游）：

```powershell
.venv\Scripts\python.exe tests\test_image_detect_e2e.py      # 引擎层 12 项断言
.venv\Scripts\python.exe tests\fake_image_upstream.py        # 单独起假上游（默认端口 8099）
```

多视口布局体检（需要 playwright，非项目声明依赖；并把 TEMP 指到可写目录）：

```powershell
$env:TEMP = "E:\dsh\Yhs-ApiDetect\.tmp_verify\pw-tmp"; $env:TMP = $env:TEMP; $env:TMPDIR = $env:TEMP
.venv\Scripts\python.exe tests\viewport_probe.py
```

## 7. 验收证据（2026-10-09 实测）

| 维度 | 方式 | 结果 |
| --- | --- | --- |
| 引擎层 | `tests/test_image_detect_e2e.py`（假上游：好模型 + 被偷换尺寸/丢透明通道/无图返回的坏模型） | 退出码 0，ALL CHECKS PASSED；任务 total=12 finished=12 passed=6 failed=6 |
| 反例识别 | 坏模型五项全部判红 | 尺寸「期望 1024x1024，实际 512x512」、透明「请求 transparent，实际存在透明像素=False」、编辑项「响应中既没有 b64_json 也没有 url」 |
| HTTP 层 | 站内 21 项检查（页面、静态资源、五个 /api/image/* 接口、越界防护、单飞 409、空凭据 400） | ok=21 bad=0；`/imgtest/` 已 404 |
| 版式 | Playwright 精确视口 390 / 414 / 520 / 768 / 1440，统计超出视口右边界且宽度 > 0 的元素 | 五档均为 0 个溢出元素，`body.scrollWidth == innerWidth` |

验收截图见 `design-previews/image-page/`。
