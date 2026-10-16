# 渠道检测实施计划

用户已批准静态设计并明确开工。新增独立 /channel 页面及 /api/channel API，旧真实性模块及数据不改。

## 基线与边界
TaskStartSnapshot：工作区 E:/Working/Yhs-ApiDetect-main；Test-Path .git=False，不可使用 git diff，修改既有集成文件前保存副本。现有 FastAPI + Jinja2 + 原生 JS/CSS。参考 design/channel-detect 下静态稿。
Change Necessity：静态稿无交互与检测能力，必须新增后端、任务、独立记录及前端。ArchitectureReviewRequired：是，重点检查旧模块隔离、凭据生命周期、结果证据。TDD Route：off/skipped，实施后离线回归。

## 契约
GET /api/channel/sources => {sources:[{id,label,family}],family_labels:{claude,gpt,gemini}}。POST /api/channel/models {base_url,api_key} => {ok,base_url,models:[{id,family}],endpoint}，失败HTTP4xx/5xx {ok:false,error:{code,message}}。
POST /api/channel/run {base_url,api_key,targets:[{model,family,claimed_source,wire_family:"gpt"|"claude"|"gemini"}],timeout:30} => {ok:true,job_id}。GET /api/channel/jobs/{id} => {id,status:running|completed|failed,completed,total,current,records:[],error?}。
GET /api/channel/records?days=7&q=地址 => {records:[]}；GET /api/channel/export?days=7&q=&format=csv|json|md；单条 GET /api/channel/records/{id}。
记录 {id,base_url,model,family,claimed_source,source_label,verdict:authentic|suspect|inconclusive|error,score:number|null,created_at,duration_ms,evidence:[],summary}。score仅已执行可计分证据通过比例，不称来源认证概率；订阅来源无法独立证明，明确未知。

## 执行
1. 后端负责 web/channel_*.py 与 tests/test_channel*.py：自有来源表、模型拉取、有限协议/能力/参数/身份/签名字段/缓存探针、逐模型容错任务、隔离记录、导出；不依赖 authenticity 内核或存储。不得真实外网付费请求。
2. 前端负责 web/templates/channel.html 与 web/static/channel.{js,css}：空初态、分族勾选、全选、弹窗来源批量/单项编辑增删、启动/进度、记录搜索导出详情；Key仅内存。
3. 主代理负责 server.py 注册路由、base.html导航、备份、契约集成、文档、验证与最终审阅。

## 验收
离线 mock 验证鉴权/不可达/限流分流、模型分族、跨族来源拒绝、坏模型不阻断、记录与导出脱敏、旧路由保留。浏览器检查主流程和窄屏、弹窗键盘、长模型名、错误可恢复。不启动替代公网GUI，不部署。独立新模块不做统计卡、保存配置、巡检或历史Tab。
