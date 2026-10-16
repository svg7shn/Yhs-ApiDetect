# OpenAI API 协议验证实现方案

## 1. 目标与边界

本方案用于检测中转站是否按 OpenAI API 协议返回合法结果。它只验证协议兼容性、基础能力和计数字段，不用模型自我身份、风格指纹、知识题来判断“真假 GPT”，因为这些不属于 OpenAI API 协议文档的可验证字段。

第一版支持两条协议：

- `responses`: `POST /v1/responses`，作为主协议。
- `chat_completions`: `POST /v1/chat/completions`，作为兼容协议。很多中转站仍以这个端点为主要接入面。

客户端继续直接用 `httpx` 访问原始 HTTP/SSE，不使用官方 SDK。原因和现有 Claude 设计一致：SDK 可能隐藏原始协议错误。

## 2. 官方依据

验证指标只来自 OpenAI 官方文档：

- API 鉴权：`Authorization: Bearer OPENAI_API_KEY`
  - https://developers.openai.com/api/reference/overview
- Models API：`GET /v1/models` 返回 list，模型对象包含 `id`、`object`、`created`、`owned_by`
  - https://developers.openai.com/api/reference/resources/models/methods/list
- Responses API：`POST /v1/responses`，响应对象 `object` 固定为 `response`，包含 `id`、`created_at`、`status`、`model`、`output`、`usage`
  - https://developers.openai.com/api/reference/resources/responses/methods/create
- Responses Streaming：`stream: true` 时使用 SSE，典型事件包括 `response.created`、`response.in_progress`、`response.output_item.added`、`response.content_part.added`、`response.output_text.delta`、`response.output_text.done`、`response.content_part.done`、`response.output_item.done`、`response.completed`
  - https://developers.openai.com/api/reference/resources/responses/methods/create
- Chat Completions：`POST /v1/chat/completions`，响应对象 `object` 固定为 `chat.completion`
  - https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
- Chat Completions Streaming：流式 chunk 的 `object` 固定为 `chat.completion.chunk`，`choices[].delta` 承载增量内容，`finish_reason` 使用官方枚举
  - https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events
- Function calling / tools：Responses API 的函数调用输出 item 使用 `type == "function_call"`；Chat Completions 使用 `message.tool_calls`，旧字段 `function_call` 已 deprecated。函数参数是 JSON 字符串，官方也要求调用方自行校验 JSON 和 schema
  - https://developers.openai.com/api/reference/resources/responses/methods/create
  - https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
- Structured Outputs：`text.format.type = "json_schema"`，`strict` 表示严格 schema 遵循
  - https://developers.openai.com/api/reference/resources/responses/methods/create

### 2.1 官方 baseline 采集

第一版实现了官方 OpenAI baseline 采集命令，用真实 OpenAI API 返回值建立对照样本。不要把 key 粘贴到聊天里；放到当前 shell 或项目 `.env` 即可：

```bash
export OPENAI_API_KEY='sk-...'
```

采集低成本完整样本：

```bash
venv/bin/relay-detector openai baseline \
  --model gpt-4o-mini \
  --wire-api both \
  --probe-set full \
  -o data/baselines/openai-gpt-4o-mini_official.json
```

只做连通性/基础协议冒烟：

```bash
venv/bin/relay-detector openai baseline \
  --model gpt-4o-mini \
  --wire-api both \
  --probe-set smoke
```

输出报告不会保存真实 API key，只保存 `api_key_masked`。每个 probe 会保留：

- `request`: 实际发送的请求体。
- `response`: 官方原始 JSON 响应。
- `headers`: 脱敏后的诊断 header，例如 `x-request-id`、`openai-processing-ms`、`x-ratelimit-*`。
- `validation`: 协议模板校验结果和扣分原因。
- `features`: 便于对比中转站的特征，如 `resp_` / `chatcmpl-` / `call_` 前缀、`usage` 子字段、tool call 是否出现、structured output 是否是 JSON object。

## 3. 检测维度

| 组 | 检测器 | 目的 | 权重 |
|---|---|---|---:|
| A | ModelsEndpointDetector | 验证鉴权、模型列表/模型对象 schema | 10% |
| A | BasicRequestDetector | 验证最小非流式请求可用 | 10% |
| B | ResponsesProtocolDetector | 验证 `/v1/responses` 非流式响应 schema | 20% |
| B | ResponsesStreamingDetector | 验证 `/v1/responses` SSE 事件和增量拼接 | 15% |
| C | ChatCompletionsProtocolDetector | 验证 `/v1/chat/completions` 非流式响应 schema | 15% |
| C | ChatCompletionsStreamingDetector | 验证 chat completion chunk schema 和 `[DONE]` | 10% |
| D | ToolCallingDetector | 验证 function tool 调用结构、参数 JSON 和 schema | 10% |
| D | StructuredOutputDetector | 验证 JSON schema 输出能力 | 5% |
| E | UsageIntegrityDetector | 验证 usage 字段存在性、非负整数、总数关系 | 5% |

`responses` 不支持时，可以通过配置只跑 `chat_completions`。被 skip 的检测器不参与总分分母。

## 4. 具体校验规则

### 4.1 ModelsEndpointDetector

请求：

```http
GET /v1/models
Authorization: Bearer <api_key>
```

校验：

- HTTP status 是 2xx。
- 顶层 `object == "list"`。
- `data` 是数组。
- 每个模型对象至少校验：
  - `id`: string
  - `object == "model"`
  - `created`: integer
  - `owned_by`: string
- 如果指定 `--model`，检查 `data[].id` 是否包含该模型；否则降级为 warning，不直接 fail。

### 4.2 BasicRequestDetector

分别按启用协议发送一个最小请求：

Responses:

```json
{
  "model": "<model>",
  "input": "Reply with exactly: pong"
}
```

Chat Completions:

```json
{
  "model": "<model>",
  "messages": [
    {"role": "user", "content": "Reply with exactly: pong"}
  ]
}
```

校验：

- HTTP status 是 2xx。
- 响应 body 是合法 JSON。
- 能提取文本内容。
- 文本包含 `pong`。这一条只作为端到端可用性，不作为模型行为指纹。

### 4.3 ResponsesProtocolDetector

针对 `POST /v1/responses` 非流式响应。

顶层字段：

- `id`: string，建议匹配 `^resp_`
- `object == "response"`
- `created_at`: number
- `status`: `completed` / `in_progress` / `incomplete` / `failed` / `cancelled` / `queued`
- `model`: string
- `output`: array
- `error`: object 或 null
- `incomplete_details`: object 或 null
- `usage`: object 或 null

完成态校验：

- 当 `status == "completed"` 时，`output` 至少包含一个 item。
- 文本输出 item：
  - `type == "message"`
  - `role == "assistant"`
  - `status == "completed"` 或 `in_progress` / `incomplete`
  - `content` 是数组
  - 文本块 `type == "output_text"`，`text` 是 string
- 拒绝块允许 `type == "refusal"`，`refusal` 是 string。

扣分：

- 必需字段缺失或类型不对：每项 -10。
- 固定枚举值错误：每项 -15。
- `status == "completed"` 但没有可解析输出：-25。
- 返回 Chat Completions 形状冒充 Responses：直接 0。

### 4.4 ResponsesStreamingDetector

请求中设置：

```json
{
  "model": "<model>",
  "input": "Reply with a short sentence.",
  "stream": true
}
```

校验 SSE：

- `Content-Type` 包含 `text/event-stream`，不满足记 warning，因为部分中转站 header 不标准但 body 可解析。
- 每个事件块有 `event:` 和 `data:`。
- `data:` 是合法 JSON。
- `data.type` 与 `event` 一致。
- 必须出现：
  - `response.created`
  - 至少一个 `response.output_text.delta` 或可等价提取文本的 delta
  - `response.completed`
- 如果出现 `response.output_text.done`，其 `text` 应等于 delta 拼接文本，或高度一致。
- 最终 `response.completed.response.object == "response"`，`status == "completed"`。

扣分：

- 无法按 SSE 解析：0。
- 缺少关键事件：每项 -20。
- delta 拼接与 done/completed 文本严重不一致：-25。
- 中途出现 JSON parse error：每次 -10，上限 -30。

### 4.5 ChatCompletionsProtocolDetector

针对 `POST /v1/chat/completions` 非流式响应。

顶层字段：

- `id`: string，建议匹配 `^chatcmpl-`
- `object == "chat.completion"`
- `created`: number
- `model`: string
- `choices`: array，至少一个元素
- `usage`: object 或缺失

`choices[]` 校验：

- `index`: number
- `message.role == "assistant"`
- 普通文本响应中 `message.content` 是 string。
- 如果响应是工具调用，OpenAI 示例中 `message.content` 可为 null，此时应存在 `message.tool_calls` 或旧字段 `function_call`。
- `finish_reason` 属于官方枚举：
  - `stop`
  - `length`
  - `tool_calls`
  - `content_filter`
  - `function_call`

扣分：

- 顶层固定字段错误：每项 -15。
- `choices` 空或不是数组：-30。
- `finish_reason` 不在枚举：-20。
- 返回 Responses 形状冒充 Chat Completions：直接 0。

### 4.6 ChatCompletionsStreamingDetector

请求中设置：

```json
{
  "model": "<model>",
  "messages": [{"role": "user", "content": "Reply with a short sentence."}],
  "stream": true,
  "stream_options": {"include_usage": true}
}
```

校验：

- 每个 data chunk 是合法 JSON，或最终 `data: [DONE]`。
- chunk 顶层：
  - `id`: string
  - `object == "chat.completion.chunk"`
  - `created`: number
  - `model`: string
  - `choices`: array
- 普通 chunk 的 `choices[].delta` 可以包含 `role`、`content`、`tool_calls`。
- 最后一个非 usage chunk 的 `finish_reason` 属于官方枚举。
- 如果请求了 `stream_options.include_usage`：
  - 最终 usage chunk 可出现 `choices: []`
  - `usage` 包含 `prompt_tokens`、`completion_tokens`、`total_tokens`
  - 允许流被中断时没有最终 usage，但记为 warning 或完整性扣分。
- 必须看到 `[DONE]`，否则扣完整性分。

### 4.7 ToolCallingDetector

Responses 请求示例：

```json
{
  "model": "<model>",
  "input": "What is the weather in Boston in celsius?",
  "tools": [{
    "type": "function",
    "name": "get_current_weather",
    "description": "Get the current weather in a given location",
    "parameters": {
      "type": "object",
      "properties": {
        "location": {"type": "string"},
        "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}
      },
      "required": ["location", "unit"]
    },
    "strict": true
  }],
  "tool_choice": "auto"
}
```

Responses 校验：

- `output[]` 中出现 `type == "function_call"`。
- `name == "get_current_weather"`。
- `call_id` 是 string。
- `arguments` 是 JSON 字符串。
- 解析后的 JSON 满足 schema。

Chat Completions 校验：

Chat Completions 工具请求必须使用 Chat 协议的工具形状，不能复用 Responses 的工具形状：

```json
{
  "model": "<model>",
  "messages": [{"role": "user", "content": "What is the weather in Boston in celsius?"}],
  "tools": [{
    "type": "function",
    "function": {
      "name": "get_current_weather",
      "description": "Get the current weather in a given location",
      "parameters": {
        "type": "object",
        "properties": {
          "location": {"type": "string"},
          "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}
        },
        "required": ["location", "unit"]
      },
      "strict": true
    }
  }],
  "tool_choice": "auto"
}
```

- `choices[0].message.tool_calls[]` 中出现 `type == "function"`。
- `function.name == "get_current_weather"`。
- `function.arguments` 是 JSON 字符串。
- 解析后的 JSON 满足 schema。
- `finish_reason == "tool_calls"`。

注意：官方文档明确提示模型可能生成无效 JSON 或额外参数，所以这里测试的是中转站是否保留 OpenAI 工具调用协议结构，不把所有 schema 偏差都归为协议错误。schema 不匹配只扣能力分。

### 4.8 StructuredOutputDetector

优先用 Responses API：

```json
{
  "model": "<model>",
  "input": "Return an object for a city named Tokyo with country and population_millions.",
  "text": {
    "format": {
      "type": "json_schema",
      "name": "city",
      "strict": true,
      "schema": {
        "type": "object",
        "properties": {
          "city": {"type": "string"},
          "country": {"type": "string"},
          "population_millions": {"type": "number"}
        },
        "required": ["city", "country", "population_millions"],
        "additionalProperties": false
      }
    }
  }
}
```

校验：

- 响应文本是合法 JSON。
- JSON 满足 schema。
- `additionalProperties: false` 下没有额外字段。

### 4.9 UsageIntegrityDetector

Responses usage:

- `input_tokens`: number
- `output_tokens`: number
- `total_tokens`: number
- `input_tokens_details.cached_tokens`: number
- `output_tokens_details.reasoning_tokens`: number
- 校验 `input_tokens + output_tokens == total_tokens`，允许未来模型出现额外 token 维度时降级为 warning。

Chat Completions usage:

- `prompt_tokens`: number
- `completion_tokens`: number
- `total_tokens`: number
- `prompt_tokens_details.cached_tokens`: optional number
- `completion_tokens_details.reasoning_tokens`: optional number
- 校验 `prompt_tokens + completion_tokens == total_tokens`。

这一项不做“字符数推 token 数”的强规则。OpenAI 文档只定义 usage 字段含义，没有给字符/token 比例；字符估算只能作为 debug 提示，不能作为正式扣分依据。

## 5. 架构改造

新增协议抽象：

```python
class ProtocolFamily(str, Enum):
    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"
    ANTHROPIC = "anthropic"
```

新增 OpenAI 原始客户端：

```python
class OpenAIClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 60):
        ...

    async def list_models(self) -> RawResponse:
        ...

    async def responses_create(self, **payload) -> RawResponse:
        ...

    async def responses_stream(self, **payload) -> AsyncIterator[SSEEvent]:
        ...

    async def chat_completions_create(self, **payload) -> RawResponse:
        ...

    async def chat_completions_stream(self, **payload) -> AsyncIterator[SSEEvent]:
        ...
```

`RawResponse` 需要保留：

- `status_code`
- `headers`
- `json`
- `text`
- `elapsed_ms`
- `request_id`: 从 `x-request-id` 读取，若中转站缺失则为空

新增目录建议：

```text
src/relay_detector/
  clients/
    anthropic.py
    openai.py
  protocols/
    openai_schema.py
    sse.py
  detectors/
    openai/
      models_endpoint.py
      basic_request.py
      responses_protocol.py
      responses_streaming.py
      chat_completions_protocol.py
      chat_completions_streaming.py
      tool_calling.py
      structured_output.py
      usage_integrity.py
```

CLI 增加：

```bash
relay-detector openai \
  --base-url https://api.openai.com \
  --api-key sk-... \
  --model <model-from-v1-models> \
  --protocol responses

relay-detector openai \
  --base-url https://some-relay.example.com \
  --api-key sk-... \
  --model <model-from-v1-models> \
  --protocol chat-completions
```

`--protocol auto` 时：

1. 先测 `/v1/responses`。
2. 如果 404/405/明确不支持，再测 `/v1/chat/completions`。
3. 两者都支持则都跑，输出双协议分数。

## 6. 评分原则

协议验证只扣文档中明确规定的字段、类型、枚举和流式事件错误。

建议分三类结果：

- `pass`: 完全符合。
- `warn`: 官方字段是 optional、或中转站 header 不标准但 body 可解析。
- `fail`: 官方固定字段、枚举、JSON/SSE 结构不符合。

总分：

```text
total_score = sum(score * weight for non-skip detectors) / sum(weight for non-skip detectors)
```

verdict:

| 分数 | 状态 |
|---:|---|
| >= 90 | excellent |
| 80-89 | passed |
| 60-79 | marginal |
| < 60 | failed |

## 7. MVP 开发顺序

1. `OpenAIClient` + SSE parser。
2. `ModelsEndpointDetector`、`BasicRequestDetector`。
3. `ResponsesProtocolDetector`、`ResponsesStreamingDetector`。
4. `ChatCompletionsProtocolDetector`、`ChatCompletionsStreamingDetector`。
5. `ToolCallingDetector`、`StructuredOutputDetector`。
6. `UsageIntegrityDetector` 和 JSON/Rich 报告。

第一版验收：

- 跑官方 `https://api.openai.com` 时，启用的协议项应 `pass` 或仅出现 optional 字段 warning。
- 对返回 Claude/Anthropic 形状、纯文本、非 SSE chunk、错误枚举值的 mock relay，应稳定 fail。
- 单测覆盖所有 schema 校验、SSE 事件顺序、`[DONE]`、usage 关系、tool arguments JSON 解析。

## 8. 与现有 Claude 设计的差异

现有 `DESIGN.md` 的 A/B 组主要验证“模型真伪”和“能力完整性”。OpenAI 版本第一阶段不做风格签名、知识题和身份自述，因为这些没有稳定的 OpenAI API 文档依据。

OpenAI 版本的核心价值是：

- 这个中转站是否真的暴露 OpenAI API 形状。
- SDK 能否按 OpenAI 协议正常消费。
- stream / non-stream / tool calling / structured output / usage 是否按官方字段返回。

后续如果要做“模型真伪”，应单独建 benchmark/evals 体系，并把依据标注为实验数据，而不是 OpenAI API 协议验证。
