# 站点读流量路由演练

栖原站点运维组（11人）在交接班前，用节点位点与会话时序复核读写路由，明确何时读副本、何时回主库、何时必须拒绝读取。

## 场景与模块

原先值班员在配置表和聊天记录中核对副本延迟，以“延迟很低”推断写后读安全。痛点：低延迟不等于已应用会话写位点、故障时回退策略不清、决策无逐请求证据。上游输入为人工确认的节点状态和请求轨迹，本站承接路由规则演练与证据留档，下游为站点值班与变更复核人员。

三个核心实体：复制节点 Node（主副角色/在线状态/日志谱系/位点/延迟），会话请求集 RequestSet（带时序的读写、提交、分行复制、故障恢复事件），路由试验 Trial（冻结输入、策略参数与结果）。提交视点演练 CommitTrial 与 Trial 共用请求集，但单独建表、单独入口，旧试验照常。6业务页+登录页，另有管理员审计。

**这是受控复制路由模型，不连接真实主从集群。** MySQL真实保存节点、请求、任务及证据；Python真实逐事件计算策略。日志位点采用命名谱系中的单调整数，未实现完整GTID集合/真实复制协议。

## 快速启动

```sh
cp .env.example .env
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```

前端 http://localhost:18247 ，后端 http://localhost:19247/docs ，MySQL localhost:17747。提交视点演练接口为 `/commit_trials`（列表/创建/详情/导出）与 `/commit_trials/policies`。

|角色|账号|密码|权限|
|---|---|---|---|
|admin|admin@qiyuan.test|Demo2026!|全部业务写入、运行与审计|
|operator|operator@qiyuan.test|Demo2026!|节点/请求编辑、运行、导出|
|viewer|viewer@qiyuan.test|Demo2026!|只读和导出，写API403|

## 一天的工作剧本

09:00 operator登录；09:05查看harbor-primary位点105、north-replica位点104/150ms、south-replica位点105/20ms；09:10打开 `QY-1117-A` 请求集；09:15确认八事件时序；09:20以100ms延迟门槛和500ms粘滞执行 `shift-20261117`；09:25检查：写入主库位点106、100ms读仍走主库、600ms南站追平106、700ms读走南站、800ms南站故障、900ms回主库、1000ms主库故障、1100ms拒绝读取；09:30导出route-1.json；09:40 viewer核验拒绝理由；10:00 admin看审计。

另有 `QY-1117-B` 高延迟副本回退主库、`QY-1117-C` 主库故障且副本未追平的拒绝案例，以及 `QY-1118-D` 订单头与商品行分行追赶案例（见下节）。所有试验使用冻结快照，不会把模型故障写回节点台账。

## 提交视点演练（订单头与商品行分开追赶）

现实里订单头与商品行常分两路复制，批次乱序到达。若查询分别取两表各自的位点，会把订单头 107、商品行 106 拼成一次并不存在的提交。新增的「提交视点」页专门演练这件事：

- **提交**：一次 commit 原子写入 orders 与 order_lines，并分配同一个单调提交位点；会话记录 required 位点。
- **复制**：`replicate` 带 `table` 与 `batch` 幂等键，可按 `(节点, 表)` 乱序到达；只有连续位点补齐后该表 watermark 才推进，缺口位点暂存。
- **同批重试**：同一节点同一 `(表, batch)` 再次到达记为 duplicate，不重复推进位点；落在连续区间内的批次幂等忽略。
- **查询**：节点视点 `viewpoint = min(订单头连续位点, 商品行连续位点)`，两表只在此视点过滤；同时核对会话 required 位点，订单在该视点不可见则拒绝。
- **主库下线策略**（由操作员选择，等待上限 `wait_ms` 可编排）：
  - `wait_then_stale`（默认）：先等副本追齐到查询时刻的提交位点；等待窗口内无追齐则退回最后一个满足会话位点的已齐旧视点，并记录落后提交数与数据龄。
  - `wait_then_reject`：等同样窗口；未追齐则拒绝读取。
  - `stale_first`：不等待，立即读取已齐旧视点（满足会话位点前提）。
- 等待不是真实睡眠：窗口内的复制/故障事件由输入时序投影计算，因此每次运行可确定复现。

页面可编排提交、复制、下线与查询事件，提交为新的请求集后冻结节点快照运行；证据页逐事件展示视点、批次去重、等待时长、落后提交数，以及“若两表各取自身位点是否会拼接”的对照。旧「路由执行」与「一致性证据」页保持 `write/read` 事件模型不变，两套演练互不覆盖。

## 真实计算规则

输入上限12节点、100事件；时间0–86400000ms，事件按at_ms非递减，时间相同按输入顺序执行。必须有且仅一个主节点，同谱系副本初始位点不能超过主库。事件字段：

```json
[
  {"at_ms":0,"kind":"write","session":"lab-A"},
  {"at_ms":100,"kind":"read","session":"lab-A"},
  {"at_ms":600,"kind":"replicate","node":"south-replica","position":106,"delay_ms":20},
  {"at_ms":700,"kind":"read","session":"lab-A"}
]
```

提交视点演练的 `QY-1118-D` 则使用：

```json
[
  {"at_ms":0,"kind":"commit","session":"lab-A","order":"SO-8801"},
  {"at_ms":20,"kind":"replicate","node":"south-replica","table":"orders","batch":"po-106","position":106},
  {"at_ms":30,"kind":"query","session":"lab-A","order":"SO-8801"},
  {"at_ms":70,"kind":"replicate","node":"south-replica","table":"order_lines","batch":"pl-107","position":107},
  {"at_ms":80,"kind":"replicate","node":"south-replica","table":"order_lines","batch":"pl-106","position":106},
  {"at_ms":90,"kind":"replicate","node":"south-replica","table":"order_lines","batch":"pl-107","position":107},
  {"at_ms":110,"kind":"node_down","node":"harbor-primary"},
  {"at_ms":950,"kind":"query","session":"lab-A","order":"SO-8802"}
]
```

- write：主库在线才成功；主库位点+1，记录该会话required_position和last_write_ms。失败写入不推进会话位点。
- read：若距最近成功写入 `< sticky_ms`，必须主库在线、同谱系且位点满足；否则拒绝。到达边界即结束粘滞。
- 粘滞期外：副本必须在线、同谱系、position≥会话required_position、delay_ms≤max_delay_ms，四条件同时成立。多个候选按延迟升序、位点降序、名称字典序选择。无副本达标则回主库；主库也失效则拒绝。
- replicate：只允许在线同谱系副本；位点不能回退、不能超过主库已提交位点。node_down/node_up修改模型状态。未知节点或非法复制在入队前422。
- 提交视点事件：commit 需 `order`；query 需 `order`（可选 `required_position`）且只在提交视点演练使用；replicate 另需 `table=orders|order_lines` 与 `batch` 幂等键。两类演练各自校验事件类型，混用返回422。
- 每个会话独立维护位点；从未成功写入的会话required=0，允许最终一致副本读取，不承诺全局强一致。低延迟绝不覆盖落后位点。

输出每事件目标/拒绝、会话所需位点、实际位点、各副本四项资格、最终节点、会话状态、汇总和SHA256。相同输入产生相同摘要；不依赖随机数或外部网络。拒绝不安全读是正常业务结果，不算任务失败。快照与结果保存在MySQL，Redis只传递任务ID。

## 工程说明

|服务|技术|职责|
|---|---|---|
|frontend|原生HTML/CSS/JS + Nginx|4业务页、编辑表单、共享组件、/api代理|
|backend|Python3.11/FastAPI/SQLAlchemy/PyJWT|校验、权限、审计、快照与导出|
|worker|Python3.11|会话时序与路由算法|
|db|MySQL8.0|真实持久化业务和JSON证据|
|redis|Redis7|持久队列|

各实体贯穿 `backend/src/{models,schemas,services,routes}` 和 `frontend/src/{api,stores,pages}`；提交视点演练的算法在 `backend/src/services/commit_routing.py`，路由/服务/页面为 `commit_trials.*` 与 `pages/commit_trials.js`、`pages/commit_evidence.js`。其余强制目录含controllers/middlewares/config/constants/utils、components/common/hooks/types/router。三组件table/badge/frame共用；hooks/session/feedback和utils/forms/html共用；request_id/errors双中间件。

共享枚举角色admin/operator/viewer与任务queued/running/completed/failed位于 `backend/src/constants/enums.py` / `frontend/src/constants/enums.js`，security/trials、frame/badge消费。节点角色primary/replica和状态healthy/down由node schema及前端表单定义，事件类型由request_set schema及路由算法定义。

环境变量见.env.example：COMPOSE_PROJECT_NAME、三端口、DB_PASSWORD、JWT_SECRET、SEED_DEMO。默认密码仅本地演示、JWT8小时；seed自然键仅补缺失，不覆盖修改。schema由metadata.create_all初始建表，无在线升级引擎。5服务均健康检查，backend/worker依赖健康db/redis，frontend依赖backend；数据库、Redis、worker产物使用命名卷。

本地开发：Python3.11安装backend/requirements.txt、配置DATABASE_URL/REDIS_URL，在backend运行 `uvicorn src.main:app --reload` 和 `python -m src.worker`，前端经Nginx同源代理。停止清空：`docker compose down -v --remove-orphans`。

限制：单worker串行、无生产任务重试/崩溃续跑；不模拟网络随机抖动、自动选主或完整GTID集合。Google Fonts离线回退，业务不依赖字体网络。

## License

MIT。`backend/tests/test_commit_view.py` 覆盖分行复制、乱序缺口、同批重试、视点查询与三种下线策略；证据与结果另存 `../../test-artifacts/gb-4108/`（若环境提供该目录）。
