# Changelog

All notable changes to **Yhs-ApiDetect** are documented here.
This project adheres to [Semantic Versioning](https://semver.org/) and the
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## [Unreleased]

## [1.2.0] - 2026-10-10

### Added

- **生图检测页 `/image` 改为站内自实现(不再外挂 Go 服务)**:整页原创重写 —— 单列纵向
  (检测参数 → 检测进度 → 逐项结果);模型支持「读价目表勾选」与「手填模型名」;
  六项测试项可多选,并实时估算「本次将跑 N 项」;结果卡逐条给出产出图缩略图、失败原因、
  逐项检查明细(默认展开,失败项整行标红)与请求/响应/图片证据下载;底部是本机检测记录。
- **检测引擎 `web/image_detect.py`**:把原 Go 版六项检测用 Python 重写(httpx + Pillow),
  覆盖文生图 transparent / opaque / auto、改图 `/v1/images/edits` 与 `/v1/edits`、
  聊天生图 `/v1/chat/completions`;判据(尺寸 / 格式 / 透明)、失败文案与证据文件命名与旧版逐条对齐。
  无账号体系、不依赖外部进程,API key 只在内存中用完即弃。
- **任务运行器 `web/image_jobs.py`**:单任务串行、进度快照、逐条结果回流,证据与 `history.jsonl`
  落在 `web_data/image/` 下按任务分目录。
- **生图检测接口**:`POST /api/image/models`、`POST /api/image/run`、
  `GET /api/image/task_status`、`GET /api/image/evidence/{task_id}/{name}`、`GET /api/image/history`。
- **离线自测**:`tests/fake_image_upstream.py`(假上游,一半正常一半故意偷换尺寸/丢透明通道/返错误结构)、
  `tests/test_image_detect_e2e.py`(九组断言)、`tests/viewport_probe.py`(视口溢出检测)。
- **`docs/IMAGE_DETECT_GO_TO_PY.md`**:Go 判据 → Python 实现的逐项对照表与本地启动说明。

### Changed

- `Yhs-ApiDetect_IMAGE_DIR` 的默认值由 `/opt/Yhs-ApiDetect/web_data/image` 改为项目内的 `web_data/image` ——
  Windows 上 `/opt` 会被解析成当前盘根的 `\opt`,本地直接起服务时建不出目录。

### Fixed

- **生图检测任务会永久卡在「检测中 / 准备中… / 0 ? 项」**:任务准备阶段建目录失败时,
  异常抛在 try 之外,任务状态永远停在 running 且全局单飞标志清不掉(后续提交全被 409 挡住)。
  现在建目录落在 try 内、finally 必复位状态,并在提交任务前预检目录,不可写时直接返回 400 与可操作文案。
- 前端轮询新增「60 秒没有出任何结果」的兜底提示;没有真正写出图片的条目不再给出打不开的链接,
  改为「产出图没写出来」占位。

### Removed

- 移除 `/imgtest` 反向代理路由与对上游 Go 服务的依赖;`web/imagetest_proxy.py` 归档到
  `_archive_20261009/`(未物理删除)。

## [1.1.1] - 2026-10-09

### Fixed

- 真实性检测页改版后，页面结构断言仍在匹配已被替换掉的 `auth-channel-list` 容器，导致 v1.1.0 的全量测试失败；断言改为校验新版的 `auth-page` 容器与 `auth-stats` 统计区

## [1.1.0] - 2026-10-09

### Added
- **渠道真实性检测页整页改版(`/authenticity`)**:连接配置 → 模型选择 → 开始检测 的三段式流程;
  新增「已启用渠道 / 检测目标 / 最近 7 天真实率 / 平均延迟」四项统计卡,模型按协议族分组勾选
  (非对话与未识别族模型默认折叠),记录列表分页(`RECORD_PAGE = 10`)与检测记录详情弹窗。
- **全站共用导航改为悬浮玻璃胶囊 + 滚动收缩**:顶部展开态 `min(1392px, 100% - 48px)`,下滚超过
  24px 收窄为 `min(1040px, 100% - 400px)` 居中(1440 视口左右留白 24px → 200px);新增
  `web/static/nav-scroll.js` 维护 `.is-scrolled`,含 1100px / 720px 断点与 `prefers-reduced-motion` 降级。
- **检测延迟自适应单位**:新增 `web/units.py` 的 `format_ms()` 与 Jinja 过滤器 `fmt_ms`(注册于
  `web/server.py`),报告页、JPG 报告与真实性检测页统一按「≥1000ms 显示秒(1 位小数)、否则显示
  毫秒、空值显示 —」渲染;阈值告警与吞吐计算仍使用原始毫秒值。
- **常见问题页**:标题层级与目录样式重做,新增页内搜索(`#faq-search`)与空结果提示。
- 真实性检测的**单请求探测超时可调**:`timeout` 限制在 5–300 秒,非法或空值回落 45 秒默认值,
  由 `web/authenticity_jobs.py` 的 `bounded()` 统一钳制。
- Caddy structured access logs(JSON 格式,含 Cf-Connecting-Ip / Referer / User-Agent)
- 用户画像分析工具 `scripts/analyze_access_log.py` — 把访问者按行为分群
  (比价决策买家 / 疑似中转站运营者 / 自测开发者 / 看了测试页未提交 / 浅浏览 / 一次性跳出 / 爬虫)
- 报告点击排行榜 + 报告评分分布直方图 + 检测可靠性 Dashboard

### Changed
- 真实性检测页文案与信息层级重写:标题改为「渠道真实性检测」,首屏直接说明「填入地址与 Key →
  读取模型列表 → 一键发起检测」,并逐项列出采集的证据类型(思考签名 / 原生协议结构 / 跨族残留 /
  自报身份 / 缓存计量 / 参数校验)。
- README:H1 + 首段加中文 SEO 关键词与锚文本回链,提升搜索引擎可见度
- GitHub repo description:关键词前置(去 emoji 占位)

### Fixed
- 本机报 `httpx.InvalidURL: Invalid port: ':1]'`(检测页黄条「请求失败」、真实性检测「读取模型列表」
  返回 500、本机 pytest 大批失败)—— 根因是环境变量 `NO_PROXY` 里**带方括号**的 IPv6 字面量
  (`[::1]`):httpx 在**构造 client 时**就把它编译成解析不了的 URL pattern,请求根本没发出去。
  新增 `src/relay_detector/proxy_env.py`,在 CLI 导入包、Web 服务启动与 pytest 启动时各清理一次并
  留一条 warning;新增 `tests/test_proxy_env.py` 锁定行为。README 故障排查里原先「Python 3.13 +
  httpx 0.28.1 + respx 的已知组合问题」的说法是错的,已更正。

### Security
- `deploy.sh` 不再内置默认部署主机,必须用 `--host user@host` 或环境变量
  `RELAY_DETECTOR_HOST` 显式提供,避免把真实服务器地址写进仓库。

## [1.0.0] - 2026-09-28

首个以 **Yhs-ApiDetect** 名义发布的版本。

### Added
- **渠道真实性检测(Web 页 `/authenticity`)** —— 按「渠道 + 模型 + 声称来源」逐项核对中转站实际提供的
  是不是它自称的那条链路:
  - 单渠道即时检测 + 全量异步巡检(同一时刻只允许一个巡检,重复启动返回 409)
  - 7 类相互独立的探针与权重:能力题(每轮动态出题,默认 3 题,40)/ 身份自报(18)/ 错误注入(12)/
    thinking 签名(10)/ 模型回显(8)/ 参数校验(7)/ 缓存行为(5)
  - 四态判定 `authentic` / `suspect` / `fail` / `error`;传输级失败单独计 `error`,**不进入真实率 /
    疑似率 / 失败率的分母**,HTTP 200 与单次超时都不单独构成判定依据
  - 与 PRD 对齐的后端规则:声称来源必填、模型可空(按来源族自动选同族模型)、跨族拒绝保存、
    配置保存走 version 乐观锁(过期 409)、执行检测只读已保存配置
  - 报告按协议族分别统计,支持 JSON / CSV 导出;探针轨迹脱敏留存(不含鉴权头,响应体截断)
  - 站点地址与 API Key 只存在浏览器 localStorage,服务端不落盘
- 新增 101 个测试用例(`tests/test_authenticity*.py`)。

### Changed
- **项目更名:Yhs-ApiDetect → Yhs-ApiDetect**(经原作者同意在其项目基础上二开)。页面标题、导航、页脚、
  FAQ 文案与文档全部换成新品牌;导航在 Gemini 与检测历史之间新增「真实性检测」入口。
  域名、Python 包名 `relay_detector`、环境变量前缀与 console script 名保持不变。
- README 重写为完整项目指南(架构、检测能力、评分体系、CLI 与 Web 路由、环境变量、测试与 CI、
  隐私与安全、部署、故障排查),并补上上游项目归属声明。

### Privacy
- 真实性检测的探针轨迹只保留脱敏请求体、截断后的响应体、状态码与耗时,不含 API Key / 鉴权头。

## [0.1.0] - 2026-05-10

### Added
- 三协议检测:Anthropic Messages API、OpenAI Chat Completions、Gemini OpenAI 兼容协议
- **Claude thinking signature 加密级真伪验证**(中转站理论上无法伪造)
- OpenAI usage 字段后端指纹识别(检测 `claude_cache_creation_*` 异源痕迹的「换芯」中转站)
- Gemini 3 thinking-by-default 适配
- 三层 needle-in-haystack 长上下文探针(32k → 1M tokens),抓「宣传 1M 实际只给 200k」式欺诈
- 加权评分 + critical issue 一票否决(单项 critical 把 verdict 上限锁在 marginal)
- 三档运行模式:quick(~$0.005) / standard(~$0.012) / full(~$0.020)
- Web 服务:FastAPI + 异步任务队列、IP 限速、报告 JPG 卡片生成
- 中转站红黑榜:按域名聚合公开报告,贝叶斯排序防 1-sample 刷分
- 永久分享链接 `/r/{id}` 与社交分享图片 `/r/{id}.jpg`
- CLI 工具 `relay-detector`:`detect` / `compare` / `ping` 三个子命令
- 官方基线收集脚本 `bench.sh`(Opus 4.7 / Sonnet 4.6 / Haiku 4.5 / Opus 4.6)

### Privacy
- API key 全程只在内存中,跑完即抹,**不落盘、不写日志、不外发**
- 报告中 key 脱敏为 `sk-y7xU••••••0h` 格式
- 无前端追踪、无 cookie、无注册

[Unreleased]: https://github.com/svg7shn/Yhs-ApiDetect/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/svg7shn/Yhs-ApiDetect/releases/tag/v1.2.0
[1.1.0]: https://github.com/svg7shn/Yhs-ApiDetect/releases/tag/v1.1.0
[1.1.1]: https://github.com/svg7shn/Yhs-ApiDetect/releases/tag/v1.1.1
[1.0.0]: https://github.com/svg7shn/Yhs-ApiDetect/releases/tag/v1.0.0
[0.1.0]: https://github.com/svg7shn/Yhs-ApiDetect/releases/tag/v0.1.0