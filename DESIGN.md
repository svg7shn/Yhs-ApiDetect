# Yhs-ApiDetect 技术设计与演进基线

## 1. 项目背景与目标

### 1.1 产品定位

对用户已获授权的 AI API 端点执行有限样本检查，报告协议兼容性、能力表现、用量字段及来源一致性线索。结论应能追溯到目标、协议、模式、时间和具体证据。

不是上游模型身份认证器，不证明物理直连、订阅类型或账单真实性。字段、身份自述、知识题、签名外观均不能独立证明模型来源；一次超时、限流或余额不足也不能直接判为伪造。

### 1.2 当前产品范围

[已验证] 以下入口在 [Web 路由](web/server.py) 与 [渠道路由](web/channel_routes.py) 中存在；入口存在不代表线上可用。

| 产品 | 页面或命令 | 范围与边界 |
| --- | --- | --- |
| Anthropic 文本检测 | /claude；CLI detect | Messages 协议及能力信号 |
| OpenAI 文本检测 | /openai；CLI detect | Chat Completions，不等于覆盖所有 OpenAI API |
| Gemini 文本检测 | /gemini；CLI detect | OpenAI 兼容路径，不是原生 generateContent |
| 渠道真实性 | /authenticity | 声称来源与模型的信号评估，独立评分 |
| 独立渠道检测 | /channel | 逐模型来源声明、作业进度、记录与导出 |
| 图片 API 检测 | /image | 文生图、编辑、聊天生图；不参与文本总分 |
| 历史与展示 | /history、/r/{job_id}、/leaderboard、/batch | 报告、历史、聚合与批量入口 |
| 辅助功能 | CLI ping、compare、openai validate、openai baseline | 连通性、报告对比、离线响应校验及基线采集 |

用户操作说明由 [README](README.md) 维护；本文只维护架构、行为边界及演进约束，不再重复整套安装教程。

### 1.3 本轮取舍

选择“对齐现有架构并列出改进”，而不是只润色旧 MVP 或同时重构代码。原因：旧稿把单协议规划和未实现开关混作现状；全面重构则超出文档任务范围。

## 2. 检测维度总览

[已验证] 配置真源分别为 [Anthropic 配置](src/relay_detector/protocols/anthropic/config.py)、[OpenAI 配置](src/relay_detector/protocols/openai/config.py)、[Gemini 配置](src/relay_detector/protocols/gemini/config.py)。下列值是权重，不是固定百分比或身份概率。

| 协议 | 检测器及权重 | 候选项数 |
| --- | --- | ---: |
| Anthropic | identity 5；behavioral_signature 15；thinking_signature 25；consistency 10；knowledge 10；pdf 8；structured_output 12；protocol 5；integrity 5；token_usage 10；message_id 5；long_context 15 | 12 |
| OpenAI | basic_request 15；model_consistency 15；function_calling 15；structured_output 15；protocol 15；integrity 15；token_billing 10；long_context 15 | 8 |
| Gemini | basic_request 15；model_info 15；function_calling 15；structured_output 15；protocol 15；integrity 15；token_usage 10 | 7 |

候选项数按权重字典键数计算；一次运行还受模式、模型适用性和显式开关影响。OpenAI 的 token_parity 不在当前默认权重字典，不能计为默认第九项。

## 3. 检测器与证据解释

### 3.1 身份、行为和知识

身份自述、风格匹配和知识题仅作为辅助信号，可能受提示词、题库、模型版本及采样影响。不得把“答对”升级为身份认证，也不得把“答错”直接等同于冒充。

实现按协议维护；Anthropic 题库随协议包分发，打包声明见 [项目配置](pyproject.toml)。更新题库应记录样本来源和适用范围；未经授权不发真实校准请求。

### 3.2 协议、工具与多模态

协议检查回答“此响应是否满足当前规则”，工具/结构化输出回答“本次输入是否得到预期结构”，PDF 检查回答“本次文档内容是否成功识别”。均不外推为完整厂商能力保证。

usage 检查针对响应字段的形态和合理性。没有独立计量或账单证据时，不应声称已经证明真实计费金额。

### 3.3 ThinkingSignatureDetector：字段检查，不是独立验签

[已验证] [当前实现](src/relay_detector/protocols/anthropic/detectors/thinking_signature.py) 使用非流式 messages_create，读取 content 中的 thinking/redacted_thinking 块与 signature 字符串。

当前评分分支：无 thinking 块为 0；有块但无签名为 30；签名少于 50 字符为 70；达到长度阈值为 100。阈值属于项目规则，不是官方密码学规范。未知模型按适用性规则跳过。

[已证伪] 旧稿将当前功能描述为“加密级真伪验证”不符合实现：本检测器没有独立密码学验证，也没有执行官方续接验签。仅凭非空字符串及长度不能证明签名真实。

[已验证] strict_signature 在 [执行配置](src/relay_detector/core/models.py) 中存在，但本轮搜索源码只发现该字段定义，未发现消费链路；不要把 --strict-signature 写为可用 CLI 功能。

需要特别区分：redacted_thinking 的协议语义与普通 thinking 不应靠同一个外观规则简单等同；当前代码共享检查分支是实现现状，不是协议正确性的证明。

### 3.4 模型一致性与长上下文

模型字符串匹配属于回显检查。当前各协议配置包含宽松前缀匹配，不能证明实际路由到该模型。建议后续明确别名映射与边界匹配，以避免相近型号被误接受。

Anthropic、OpenAI 的长上下文检测为显式启用项；Gemini 当前没有对应配置项。成功只说明该次探针在所测输入规模下通过；失败应区分输入上限、网络、限流、额度与内容提取。

## 4. 技术架构

### 4.1 分层与依赖

[已验证] 当前结构是“CLI/Web 编排 → 协议装配 → 共享 Runner → 协议客户端/检测器 → 报告”，不是三个独立进程。

| 层 | 责任 | 代码依据 |
| --- | --- | --- |
| CLI | 参数解析、协议选择、命令与报告输出 | [cli.py](src/relay_detector/cli.py) |
| Web | 请求校验、提交、状态与展示入口 | [server.py](web/server.py) |
| 共享核心 | 数据模型、检测器抽象、调度与评分 | [runner.py](src/relay_detector/core/runner.py)、[scorer.py](src/relay_detector/core/scorer.py) |
| 协议层 | 客户端、模式配置、协议检测器和装配 | [Anthropic runner](src/relay_detector/protocols/anthropic/runner.py)、[OpenAI runner](src/relay_detector/protocols/openai/runner.py)、[Gemini runner](src/relay_detector/protocols/gemini/runner.py) |
| 独立业务 | 渠道与图片的任务、记录和证据 | [channel_jobs.py](web/channel_jobs.py)、[image_jobs.py](web/image_jobs.py) |

约束：共享核心不承担厂商特有规则；协议差异留在协议模块。保留兼容导入，不在本轮删除旧入口。例如 [旧 runner](src/relay_detector/runner.py) 和 [旧签名检测入口](src/relay_detector/detectors/thinking_signature.py) 已是转导出兼容层，不应再写第二份逻辑。

### 4.2 核心数据模型

[已验证] 真源为 [core/models.py](src/relay_detector/core/models.py)。

- ExecutionConfig：模式、并发、超时、长上下文及保留配置字段。
- DetectorResult：name、status、score、weight、details、duration_ms、error；status 为 pass/fail/skip/error。
- DetectionReport：protocol、目标、模式、时间、结果、总分、verdict、performance、run_error 等。
- PerformanceMetrics：总耗时、TTFT、usage、请求数、退避数等；字段存在不代表所有运行都会填满。

历史报告默认 protocol 为 anthropic；tier 默认 cryptographic。这是兼容默认值，不是密码学证明。未来更名须兼容旧报告，不能在文档修订中暗改序列化契约。

### 4.3 Active / Passive 接口

[已验证] [检测器基类](src/relay_detector/core/detectors_base.py) 统一接口；Active 发起请求，Passive 接收客户端广播并在 finalize 阶段汇总。共享 Runner 按类型分组，不为每个被动检查重复发送同一请求。

新增检测器应同时核对注册、权重、模式成员、模型适用性及测试，不能仅新建文件就算接入。

### 4.4 客户端边界

各协议客户端负责请求与流式处理，节流包装负责并发控制、被动广播和指标累计。失败证据不应夹带凭据；错误文本、URL、响应片段都要纳入脱敏边界。

## 5. 评分体系

### 5.1 单项与结论含义

[已验证] 单项分值范围为 0–100，权重非负。分数代表当前规则下的表现，不是“真实模型的概率”。不同协议、模式及规则版本的分数不可直接视为同一标尺。

### 5.2 总分与 skip

[已验证] [compute_total](src/relay_detector/core/scorer.py) 的现行公式：

总分 = Σ(score × weight，排除 skip) / Σ(weight，排除 skip)。

没有有效项或总权重不大于零时返回 0。error 仍在分母内，并非自动排除；此现状由 [评分测试](tests/test_scorer.py) 明确覆盖。展示层必须优先解释运行错误，不能把运输失败包装为模型质量结论。

### 5.3 verdict 阈值与封顶

[已验证] 分数 ≥70 为 passed；50–70（不含 70）为 marginal；低于 50 为 failed。存在 critical finding 时，passed 被封顶为 marginal，总分本身不因此重算。

余额、用量限制及模型不可用等错误由 fatal_run_error 识别；[Web 作业](web/jobs.py) 在构建报告前调用它，将 run_error 写入报告。该识别目前基于文本模式，不等于完整的错误分类系统。

渠道真实性、独立渠道和图片检测有自己的记录与解释，不能直接套入文本 compute_total。排行榜也应带协议、模式及样本边界，不能解读为供应商认证排名。

## 6. 执行策略与资源控制

### 6.1 三档模式

[已验证] 下表是模式字典的成员数量，不是请求次数。

| 协议 | quick | standard | full |
| --- | ---: | ---: | ---: |
| Anthropic | 5 | 9 | 12 |
| OpenAI | 3 | 7 | 8 |
| Gemini | 3 | 7 | 7 |

长上下文仍需显式开启，full 并不表示自动执行所有高成本探针。基础并发配置为 3；for_mode 的整体超时默认 quick 60 秒、standard 120 秒、full 180 秒。Web 长上下文普通/极限档分别至少调整为 300/900 秒，见 [Web 作业配置](web/jobs.py)。这些是配置，不是完成时长或费用承诺。

### 6.2 调度与响应复用

[已验证] [共享 Runner](src/relay_detector/core/runner.py) 先按模式和 applies_to 选择检测器，为未选项输出 skip；注入配置后并发执行 Active；最后汇总 Passive 和性能指标。

当前 wait_for 包围 Active gather。整体超时会为所有 Active 重建 error 结果，已完成子项的结果可能无法保留。建议后续单独收集已完成结果，仅给未完成项标记超时。

### 6.3 TTFT 与耗时口径

[已验证] 没有流式 TTFT 样本时，协议 Runner 提供额外最小流式探针；因此 quick 也可能产生额外请求。TTFT 取已采样最小值，不是均值或 P95。

当前 total_latency_ms 在补充 TTFT 探针之前计算，不能声称它覆盖整个端到端作业。建议分别记录 active_elapsed、probe_elapsed 与 wall_elapsed，并明确计费请求数口径。

### 6.4 缓存与保留字段

[已验证] use_cache、persist_cache 与 strict_signature 出现在执行配置定义中；本轮源码搜索未发现其消费实现。旧稿的短期结果缓存、强制刷新缓存和严格验签不能作为当前可用能力宣传。

建议：未实现字段要明确标注保留，后续选择实现或经兼容评审弃用。任何结果缓存都必须包含协议、目标、模式、检测器/规则版本和影响结果的参数，且不能存储明文密钥。

### 6.5 作业、记录与重启

[已验证] 文本 [jobs.py](web/jobs.py) 使用进程内作业管理及报告落盘；[channel_jobs.py](web/channel_jobs.py) 使用进程内字典与 asyncio task，限制同时运行 3 个作业、最多保留 100 个作业条目。后者每个目标结果写入 [SQLite store](web/channel_store.py)，记录上限 5000。

渠道运行状态 running/completed/failed 与单项检测结论不同。一个作业 completed 只代表处理结束，不代表所有模型通过。进程内任务不具备跨进程统一调度或重启续跑保证。

图片使用独立 [图片作业](web/image_jobs.py) 管理器与证据产物，不应合并到文本任务的分数或执行统计。

## 7. 文件职责与文档真源

| 维护对象 | 真源/入口 | 规则 |
| --- | --- | --- |
| 安装、使用、常见问题 | [README](README.md) | 用户操作入口 |
| 当前架构与改进边界 | 本文 | 与代码核对，区分当前/建议 |
| 历史协议拆分设计 | [v2 重构草案](docs/REFACTOR_v2_PROTOCOLS.md) | 历史动机参考，不把旧阶段状态当现状 |
| 渠道使用 | [渠道说明](docs/channel-detection.md) | 独立模块操作与数据边界 |
| 渠道原设计与验收 | [计划](docs/channel-detection-plan.md)、[验收](docs/channel-detection-acceptance.md) | 变更时核对，不复制成新的平行真源 |
| 安全说明 | [SECURITY](SECURITY.md) | 对外部署前必须核对 |
| OpenAI 响应校验 | [协议校验说明](OPENAI_API_PROTOCOL_VALIDATION.md) | 与常规 detect 区分 |

运行时数据、用户上传、真实凭据、历史探测结果不应作为源码或公开测试夹具提交。本文未读取本机历史报告、数据库内容或凭据。

## 8. 技术栈与运行假设

[已验证] [项目元数据](pyproject.toml) 声明 Python ≥3.10；核心依赖包含 httpx、Pydantic、Typer、Rich、rapidfuzz；Web 可选依赖包含 FastAPI、Uvicorn、Jinja2、Pillow、python-multipart。开发依赖包含 pytest、pytest-asyncio、respx。

部署假设：当前内存任务/限流组件不应被描述为分布式队列。多 worker 或多副本上线前需要验证任务可见性、全局并发与记录一致性；单机可运行不等于具备多租户隔离。

## 9. 数据与安全边界

仅检测获得授权的端点。真实请求可能计费；不自动扩大模型列表、输入体积或长上下文档位。

建议作为所有入口的统一验收约束：

- 凭据只在请求与任务必要生命周期内使用；日志、报告、异常、导出不保留明文凭据。
- 输入 URL、重定向、远程图片下载均须应用一致的网络访问策略；不得让公开服务任意访问内部地址。
- 用户可见报告、历史、证据文件和删除接口必须有适合部署场景的访问控制。
- 导出处理 CSV 公式和 HTML/Markdown 注入；证据文件路径不能越出产物目录。
- 明确取消、保留周期和删除边界；删除历史数据前遵守备份和授权要求。

[未验证] 本轮不是全量安全审计，不能据此承诺上述约束已经覆盖所有入口。[渠道存储](web/channel_store.py) 已有字段白名单、脱敏与 CSV 处理代码，不代表其他模块自动获得相同保护。

## 10. 优化路线与验收标准（未实施）

| 优先级 | 建议 | 验收条件 |
| --- | --- | --- |
| P0 | 统一证据文案 | CLI、Web、报告不再将签名外观检查描述为独立密码学验签；旧 tier 可读且解释清楚 |
| P0 | 运行有效性与质量分离 | 超时、余额不足、无可用模型有独立错误原因；有效项与错误项数可复算；旧评分迁移明确 |
| P0 | 超时保留部分成果 | 一个检测器超时时，已完成结果不被覆盖；取消后不继续发送新请求 |
| P1 | 校准指标口径 | 端到端耗时包含补充探针；TTFT 标明最小值；请求和重试计数有可复算测试 |
| P1 | 收紧模型匹配 | 别名/快照合法映射通过，相近但不同型号拒绝；覆盖空值、前缀和分隔符 |
| P1 | 规则与报告版本化 | 新报告携带 schema/rules/model-profile 版本，旧报告读取行为明确；不得隐式重算旧成绩 |
| P1 | 梳理作业公共接口 | 先统一状态和错误契约，再评估复用；不强行合并渠道/图片业务评分 |
| P2 | 模型配置来源登记 | 每项参数有来源、核验日期和未知状态；官方属性与项目假设分开 |
| P2 | 可选持久化任务 | 仅在多 worker/重启恢复需求确定后设计租约、幂等、取消与凭据生命周期 |

顺序：先纠正文案与有效性，再改善调度和指标，最后处理持久化扩展。不得以优化为名直接删除兼容入口或更改历史数据。

## 11. 验证与交付边界

文档验收包括：本地链接存在性；模式/权重与源码一致性；关键评分行为的离线测试；原稿备份可回读。

建议回归入口为 [评分测试](tests/test_scorer.py)、[协议布局测试](tests/test_v2_protocol_layout.py)、[协议隔离测试](tests/test_protocol_isolation.py)、[CLI 协议解析测试](tests/test_cli_protocol_resolve.py)。本轮实际执行情况以交付说明为准，不将测试文件存在当作测试通过。

[未验证] 本轮不执行真实 API 探测，不验证部署页面、费用、厂商最新模型规格，也不宣称全面安全审计或全量测试通过。

## 附录 A：旧稿纠偏记录

- 单 Claude MVP → 当前多协议与独立 Web 产品范围。
- “签名有效/明确假冒” → 签名字段及长度观测，来源不获认证。
- 固定百分比 → 按有效项归一化的权重。
- 计划中的缓存/严格验签开关 → 配置保留字段与未接入状态。
- 固定耗时/费用 → 仅说明配置和观测口径，不承诺真实结果。
- 老单层目录 → core、protocols、入口与兼容层。
- 旧开发里程碑 → 带验收标准的待实施改进。

## 附录 B：模型参数维护

模型目录以各协议配置为代码真源，但“代码中配置了某模型”不证明厂商正式发布或支持相关参数。本轮未核验最新外部文档，因此不继续保留旧稿“全部官方交叉验证”的总括声明，也不复制过期 cutoff、价格、页数与 token 上限表。

后续更新模型参数时，应附可复核来源及日期；未知能力以未知处理，不凭名称推断支持。真实校准请求需要独立授权和费用确认。
