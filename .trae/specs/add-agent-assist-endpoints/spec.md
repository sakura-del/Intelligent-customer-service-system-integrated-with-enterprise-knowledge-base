# 坐席辅助端点 Spec

## Why

当前系统已具备完整的智能客服链路（多 Agent 协同 + RAG + 人工转接决策），但当 EscalationEngine 决定转人工后，**人工客服一侧没有任何 API 支撑**：坐席无法看到待接入会话队列、无法接手会话、无法在原会话上下文中继续回复、无法快速调用知识库与业务系统辅助应答、也无法把人工方案沉淀回知识库。

这导致「转接」实际上是断点的：用户被转接后人工客服只能从零开始询问，已生成的 EscalationCard 无处消费，对话上下文也无法在坐席侧延续，严重削弱了人机协同价值，是当前最大的功能短板。

本 Spec 通过新增一组 `/api/v1/agent/*` 端点，补齐坐席侧工作台的后端能力，形成「机器人 → 转接 → 坐席接手 → 坐席辅助应答 → 方案沉淀回库」的完整闭环。

## What Changes

### 新增端点（8 个）

| 端点 | 方法 | 用途 |
|------|------|------|
| `/api/v1/agent/sessions/pending` | GET | 列出待接入会话（含 EscalationCard 摘要） |
| `/api/v1/agent/sessions/{session_id}` | GET | 查看会话详情（EscalationCard + 完整 history） |
| `/api/v1/agent/sessions/{session_id}/accept` | POST | 坐席接手会话（写入 assigned_agent_id） |
| `/api/v1/agent/sessions/{session_id}/messages` | POST | 坐席在原会话上下文中发消息（追加 history） |
| `/api/v1/agent/sessions/{session_id}/knowledge-recommend` | POST | 复用 HybridRetriever 推荐相关知识 |
| `/api/v1/agent/sessions/{session_id}/business-assist` | POST | 复用 BusinessAgent 查询业务系统（含脱敏） |
| `/api/v1/agent/sessions/{session_id}/resolve` | POST | 标记会话已解决（agent_status=resolved） |
| `/api/v1/agent/sessions/{session_id}/solution` | POST | 录入人工方案，沉淀为 FAQ 候选（复用 KnowledgeFeedback） |

### 会话状态字段扩展（**BREAKING** 仅对内部状态字典，API 向后兼容）

`SessionManager._default_session_state` 新增 3 个字段：
- `agent_status: str`（"pending" / "assigned" / "resolved"）
- `assigned_agent_id: Optional[str]`
- `escalation_card: Optional[dict]`（缓存转接时生成的 EscalationCard，避免重复构建）

转接发生时由 `EscalationEngine` 或调用方写入 `agent_status="pending"` 并缓存 `escalation_card`。

### 新增 Schemas

- `AgentSessionSummary`：待接入列表条目（session_id / user_id / priority / reason / turn_count / created_at / agent_status）
- `AgentSessionDetail`：会话详情（含 EscalationCard + history 列表 + agent_status + assigned_agent_id）
- `AgentMessageRequest`：坐席发消息请求体（content）
- `AgentMessageResponse`：坐席发消息响应（message_id / timestamp）
- `KnowledgeRecommendRequest` / `KnowledgeRecommendResponse`：知识推荐请求/响应
- `BusinessAssistRequest` / `BusinessAssistResponse`：业务辅助请求/响应
- `SolutionSubmitRequest`：方案沉淀请求（复用现有 KnowledgeFeedback.record_human_solution）
- `ResolveRequest`：标记已解决请求（可选备注）

### 新增 API 模块

- `app/api/v1/agent.py`：坐席辅助端点统一入口，使用 `APIRouter(prefix="/api/v1/agent", tags=["坐席辅助"])`，依赖 `verify_api_key`

## Impact

### 受益能力

- 人工转接闭环：补齐 `EscalationEngine.build_card` 产出的卡片消费方
- 知识库治理：人工方案回流复用 `KnowledgeFeedback` 已有的审核入库链路
- 业务系统集成：坐席辅助查询复用 `BusinessAgent.execute` 含脱敏逻辑

### 受影响代码

- `app/api/v1/agent.py`（新建）
- `app/api/v1/__init__.py` 或 `app/main.py`（注册新 router）
- `app/core/session.py`（`_default_session_state` 新增 3 字段，新增 `list_pending_sessions` / `assign_agent` / `resolve_session` 方法）
- `app/schemas/agent.py`（新建，存放上述新 schema）
- `app/agents/escalation.py`（`build_card` 时同步写入 session 状态，可选；或在转接调用方写入）
- `README.md`（新增端点说明，遵循用户约束）

### 不受影响

- 路由文件中现有端点不变
- 现有 `EscalationCard` schema 完全复用，不修改
- `HybridRetriever` / `BusinessAgent` / `KnowledgeFeedback` 接口签名不变

## ADDED Requirements

### Requirement: 待接入会话列表

系统 SHALL 提供 `GET /api/v1/agent/sessions/pending` 端点，返回所有 `agent_status="pending"` 的会话摘要，按 EscalationPriority 降序排列，便于坐席按优先级接手。

#### Scenario: 存在待接入会话
- **WHEN** 转接已触发且尚未被坐席接手
- **THEN** 返回 200，body 为 `List[AgentSessionSummary]`，每条含 session_id / user_id / priority / escalate_reason / turn_count / created_at

#### Scenario: 无待接入会话
- **WHEN** 没有任何 pending 状态会话
- **THEN** 返回 200，body 为空列表 `[]`

#### Scenario: 鉴权失败
- **WHEN** 请求未携带有效 `X-API-Key`
- **THEN** 返回 401

### Requirement: 会话详情查询

系统 SHALL 提供 `GET /api/v1/agent/sessions/{session_id}` 端点，返回 `AgentSessionDetail`，包含缓存的 EscalationCard（无缓存时即时调用 `EscalationEngine.build_card` 重建）、完整 history、当前 agent_status 与 assigned_agent_id。

#### Scenario: 会话存在
- **WHEN** session_id 有效
- **THEN** 返回 200，body 含 escalation_card / history / agent_status / assigned_agent_id

#### Scenario: 会话不存在
- **WHEN** session_id 无效
- **THEN** 返回 404

### Requirement: 坐席接手会话

系统 SHALL 提供 `POST /api/v1/agent/sessions/{session_id}/accept` 端点，将 agent_status 置为 "assigned"，写入 assigned_agent_id。已被接手（assigned/resolved）的会话再次接手返回 409。

#### Scenario: 首次接手成功
- **WHEN** 会话处于 pending 状态
- **THEN** agent_status 改为 assigned，返回 200 与更新后的 AgentSessionDetail

#### Scenario: 重复接手冲突
- **WHEN** 会话已为 assigned 或 resolved
- **THEN** 返回 409，错误信息含当前 assigned_agent_id

### Requirement: 坐席发送消息

系统 SHALL 提供 `POST /api/v1/agent/sessions/{session_id}/messages` 端点，将坐席消息以 role="assistant" 追加到 session history，供后续上下文延续。仅允许在 assigned 状态下发送，否则返回 409。

#### Scenario: 发送成功
- **WHEN** 会话为 assigned 状态且 content 非空
- **THEN** 追加到 history，返回 200 与 AgentMessageResponse（message_id / timestamp）

#### Scenario: 未接手发送
- **WHEN** 会话为 pending 或 resolved
- **THEN** 返回 409

### Requirement: 知识推荐辅助

系统 SHALL 提供 `POST /api/v1/agent/sessions/{session_id}/knowledge-recommend` 端点，复用 `HybridRetriever.retrieve`，根据坐席输入的查询返回 Top-K 相关知识片段，供坐席快速参考。

#### Scenario: 命中知识
- **WHEN** 查询在知识库中有相似内容
- **THEN** 返回 200，body 为 `KnowledgeRecommendResponse`，含 chunks 列表（content / score / source）

#### Scenario: 未命中
- **WHEN** 检索得分全部低于阈值
- **THEN** 返回 200，chunks 为空列表

### Requirement: 业务查询辅助

系统 SHALL 提供 `POST /api/v1/agent/sessions/{session_id}/business-assist` 端点，复用 `BusinessAgent.execute`，坐席输入自然语言查询业务系统（订单/会员/退换货），返回结构化结果（含敏感字段脱敏）。

#### Scenario: 查询成功
- **WHEN** 业务查询可解析
- **THEN** 返回 200，body 为 `BusinessAssistResponse`，含 result / masked_fields

#### Scenario: 查询失败
- **WHEN** 业务系统不可达或参数非法
- **THEN** 返回 200，result 含降级错误说明，不抛 5xx

### Requirement: 标记会话已解决

系统 SHALL 提供 `POST /api/v1/agent/sessions/{session_id}/resolve` 端点，将 agent_status 置为 "resolved"，可选写入 resolve_note。仅 assigned 状态可标记已解决，否则返回 409。

#### Scenario: 标记成功
- **WHEN** 会话为 assigned 状态
- **THEN** agent_status 改为 resolved，返回 200

#### Scenario: 未接手直接解决
- **WHEN** 会话为 pending
- **THEN** 返回 409

### Requirement: 人工方案沉淀

系统 SHALL 提供 `POST /api/v1/agent/sessions/{session_id}/solution` 端点，复用 `KnowledgeFeedback.record_human_solution`，将坐席录入的方案作为 FAQ 候选，进入 pending 审核队列。

#### Scenario: 录入成功
- **WHEN** 坐席提交 question + solution
- **THEN** 调用 KnowledgeFeedback 入队，返回 200 与 HumanSolutionRecord（含 solution_id）

#### Scenario: 内容为空
- **WHEN** question 或 solution 为空字符串
- **THEN** 返回 422

## MODIFIED Requirements

### Requirement: 会话状态管理

SessionManager 的会话状态字典在原有字段基础上，新增 `agent_status` / `assigned_agent_id` / `escalation_card` 三个字段。新会话创建时 `agent_status` 默认为 None（表示尚未触发转接，区别于 "pending" 已转接待接入）。当 EscalationEngine 决定转接时，调用方需将 `agent_status` 置为 "pending"。

SessionManager 新增方法：
- `list_pending_sessions() -> List[Dict]`：返回所有 agent_status="pending" 的会话摘要
- `assign_agent(session_id, agent_id) -> bool`：原子化接手，CAS 判断 pending → assigned
- `resolve_session(session_id, note: Optional[str] = None) -> bool`：原子化解决，CAS 判断 assigned → resolved

## REMOVED Requirements

无移除项。
