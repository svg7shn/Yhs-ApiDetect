# 渠道检测离线验收报告

## 交付范围
独立入口 `/channel` 位于真实性检测之后；独立 `/api/channel` 后端、记录存储与导出，保留旧真实性检测。支持连接校验、三族模型选择、来源调参弹窗、逐模型执行与错误继续、进度、地址搜索及 CSV / JSON / MD 导出。

## 已验证
- 主代理执行 `python -m pytest tests/test_authenticity.py tests/test_authenticity_api.py tests/test_authenticity_store.py tests/test_authenticity_jobs.py tests/test_channel_backend.py tests/test_channel_integration.py -q`：163 passed, 1 warning in 1.33s。任务 pwsh-61，exit code 0。警告为 Starlette 引用 anyio.abc.BlockingPortal 的弃用提示。
- `node --check web/static/channel.js`：同一任务 exit code 0。
- `python scripts/channel_browser_check.py`：任务 pwsh-58，exit code 0。正式 Jinja 模板、模块 CSS/JS配合模拟 API；验证默认空选、来源必填、弹窗焦点、提交唯一性、进度解锁、连接/协议变更失效与原生族过滤。
- 浏览器实际视口为 1424×1405、1424×1005、524×1405；截图已亲读，无横向溢出。截图是模拟数据，不是真实上游结果。
- 协议与模型族分离：默认 OpenAI 兼容，支持显式原生协议；不自动换协议付费重试。截断、拒绝和安全拦截归为未知证据，不直接误判异常。

## 证据文件
- [桌面截图](../artifacts/channel-check/desktop.png)
- [参数弹窗](../artifacts/channel-check/parameters.png)
- [窄屏截图](../artifacts/channel-check/mobile.png)
- [使用说明](channel-detection.md)

## 未验证与限制
未调用真实上游或使用真实 Key，未进行整站真实端到端验证，未部署；390px 视口未验证。浏览器验证不包含全站 app.js。原 browser-check.log 被文本读取器识别为二进制，不作为本报告的可读日志证据，采用任务退出状态与浏览器断言及截图。

协议、能力及签名字段存在不能证明订阅来源；2026-10-14 起判定改为加权分制（见下节），会给出「来源一致」，但订阅 / 合作伙伴类来源强制回落「证据不足」。分数为加权通过率，不是来源真实性概率。记录接口没有用户隔离，公开部署前须加访问控制。

## 2026-10-14 更新：加权判定与五列检测明细

判定口径改为加权分：有效检查 ≥3 项且加权分 ≥75 判「来源一致」（订阅 / 合作伙伴类来源强制回落「证据不足」），≥50 判「疑似异常」，<50 判「来源不符」；有效检查不足 3 项时不给结论。检测证据由逐条卡片改为五列明细表（信号 / 类别 / 结果 / 权重 / 说明），旧记录缺少 category / weight / id 时显示占位符。

已验证：
- `python -m pytest tests/ -q`：513 passed, 1 warning（含新增 5 个判定测试）。
- `node --check web/static/channel.js`：exit 0。
- `python scripts/channel_browser_check.py`：exit 0。四个场景断言数 desktop 32 / parameters 24 / mobile 24 / records 8，全部通过；新增断言覆盖记录卡片徽标文案「来源一致」、证据级 / 得分 / 能力元信息、五列表头、9 行证据、信号 id、权重值、信息性行与旧记录占位符。
- 截图 [records.png](../artifacts/channel-check/records.png) 已亲读：弹窗内五列表格结构、类别标签、通过 / 信息徽标与权重列均符合预期。

未验证与限制：仍未调用真实上游；截图使用模拟数据；390px 视口未验证。
