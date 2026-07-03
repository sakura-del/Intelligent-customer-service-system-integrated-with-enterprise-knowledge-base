# Tasks

> change-id: add-agent-assist-endpoints
> 预估工时：3-5 天
> 依赖关系：Task 2/3/4/5/6/7 均依赖 Task 1（SessionManager 扩展）与 Task 2（schemas）

- [x] Task 1: 扩展 SessionManager 会话状态字段与新增坐席管理方法
  - [x] SubTask 1.1: `app/core/session.py` 的 `_default_session_state` 新增 `agent_status`（默认 None）/ `assigned_agent_id`（默认 None）/ `escalation_card`（默认 None）3 个字段
    - 证据：`app/core/session.py:254-263` 新增 4 字段（含 resolve_note）
  - [x] SubTask 1.2: 新增 `list_pending_sessions() -> List[Dict]`，返回所有 `agent_status="pending"` 的会话摘要，按 EscalationPriority 降序
    - 证据：`app/core/session.py:193-226`
  - [x] SubTask 1.3: 新增 `assign_agent(session_id, agent_id) -> bool`，CAS 判断 `pending → assigned`
    - 证据：`app/core/session.py:228-244`
  - [x] SubTask 1.4: 新增 `resolve_session(session_id, note) -> bool`，CAS 判断 `assigned → resolved`
    - 证据：`app/core/session.py:246-264`
  - [x] SubTask 1.5: 新增 `mark_pending(session_id, escalation_card) -> bool`
    - 证据：`app/core/session.py:266-281`
  - [x] SubTask 1.6: `update_session` 已存在，能正确写入新字段（无需改动）
    - 证据：`app/core/session.py:88-106` 通过 `**fields` 直接写入

- [x] Task 2: 创建 `app/schemas/agent.py` 数据模型
  - [x] SubTask 2.1: `AgentSessionSummary`（8 字段）
  - [x] SubTask 2.2: `AgentSessionDetail`（含 escalation_card / history / agent_status / assigned_agent_id / turn_count / emotion_score）
  - [x] SubTask 2.3: `AgentMessageRequest`（min_length=1）/ `AgentMessageResponse`（message_id / timestamp / role）
  - [x] SubTask 2.4: `KnowledgeRecommendRequest`（query / top_k 默认 5）/ `KnowledgeRecommendResponse` / `KnowledgeChunk`
  - [x] SubTask 2.5: `BusinessAssistRequest` / `BusinessAssistResponse`（result / masked_fields）
  - [x] SubTask 2.6: `SolutionSubmitRequest`（question / solution / intent Optional），响应复用 `HumanSolutionRecord`
  - [x] SubTask 2.7: `ResolveRequest`（note Optional）/ `ResolveResponse`（session_id / agent_status / resolved_at）
  - [x] 额外：`AcceptRequest`（agent_id 默认 agent-default）
  - 证据：`app/schemas/agent.py` 全部 13 个模型导入验证通过

- [x] Task 3: 创建 `app/api/v1/agent.py` 端点骨架并注册 router
  - [x] SubTask 3.1: `app/api/v1/agent.py` 文件创建，定义 `APIRouter(prefix="/api/v1/agent", tags=["坐席辅助"], dependencies=[Depends(verify_api_key)])`
  - [x] SubTask 3.2: 在 `app/main.py` 中注册新 router（第 14 行 import，第 68-69 行 include_router，紧随 escalation.router 之后）
  - [x] SubTask 3.3: 应用初始化成功，router 已挂载（空骨架符合预期，端点在 Task 4-7 填充）

- [x] Task 4: 实现待接入会话列表与会话详情端点
  - [x] SubTask 4.1: `GET /sessions/pending` 已实现，无需字段映射（list_pending_sessions 返回 key 与 AgentSessionSummary 完全对齐）
  - [x] SubTask 4.2: `GET /sessions/{session_id}` 已实现，404/缓存重建/写回均落地
  - [x] SubTask 4.3: pending 列表按 EscalationPriority 降序（由 SessionManager.list_pending_sessions 保证）
  - 证据：`app/api/v1/agent.py` 8 个端点全部注册，OpenAPI 验证通过

- [x] Task 5: 实现坐席接手与发消息端点
  - [x] SubTask 5.1: `POST /sessions/{session_id}/accept` 已实现，AcceptRequest 默认 agent-default，CAS 失败返回 409
  - [x] SubTask 5.2: `POST /sessions/{session_id}/messages` 已实现，非 assigned 返回 409，append_history 后返回 AgentMessageResponse
  - [x] SubTask 5.3: 验证 history 追加 role=assistant（append_history 复用现有方法）
  - 证据：`app/api/v1/agent.py`

- [x] Task 6: 实现知识推荐与业务辅助端点
  - [x] SubTask 6.1: `POST /sessions/{session_id}/knowledge-recommend` 已实现，复用 HybridRetriever.retrieve，异常降级空列表
  - [x] SubTask 6.2: `POST /sessions/{session_id}/business-assist` 已实现，复用 BusinessAgent.execute，异常降级 result.error
  - [x] SubTask 6.3: 业务辅助返回中敏感字段已脱敏（BusinessResult.data 已脱敏，masked_fields 自动提取）
  - 证据：`app/api/v1/agent.py`

- [x] Task 7: 实现标记已解决与方案沉淀端点
  - [x] SubTask 7.1: `POST /sessions/{session_id}/resolve` 已实现，CAS 失败返回 409，返回 ResolveResponse
  - [x] SubTask 7.2: `POST /sessions/{session_id}/solution` 已实现，复用 KnowledgeFeedback.record_human_solution，返回 HumanSolutionRecord
  - [x] SubTask 7.3: solution 端点提交后可在 `GET /api/v1/escalation/solutions/pending` 查到（复用现有审核链路，KnowledgeFeedback 单例共享）
  - 证据：`app/api/v1/agent.py`

- [x] Task 8: 在 EscalationEngine 触发转接时写入 session 状态
  - [x] SubTask 8.1: 调研确认转接决策落地在 `graph.py:escalate_node`（LangGraph 路径与同步降级路径均经过此节点）
  - [x] SubTask 8.2: 在 `escalate_node` 中卡片生成后调用 `session_manager.mark_pending(session_id, escalation_card)`，失败时仅记录日志不阻断主链路
    - 证据：`app/agents/graph.py:627-636` 新增 mark_pending 调用
  - [x] SubTask 8.3: 端到端验证通过：构造触发转接的 state 后，session 的 agent_status 变为 pending，escalation_card 已缓存

- [x] Task 9: 编写测试用例
  - [x] SubTask 9.1: `tests/test_agent_assist.py` 覆盖 8 个端点的正常路径与边界（404 / 409 / 422），共 21 个端点测试
  - [x] SubTask 9.2: SessionManager 扩展方法单元测试 7 个（list_pending_sessions 排序 / assign CAS / resolve CAS / mark_pending）
  - [x] SubTask 9.3: 模块级 fixture 强制 `LANGFUSE_ENABLED=False` + `SMALL_LLM_API_KEY=""` + `LLM_API_KEY=""`，重置 13 个单例
  - [x] SubTask 9.4: 全量回归 667 passed / 1 flaky（test_stream_chitchat_uses_quick_intent_fast_first_token 性能断言，单独重跑通过，与本次改动无关）
  - 证据：`tests/test_agent_assist.py` 28 个用例全部通过

- [x] Task 10: 更新 README.md
  - [x] SubTask 10.1: 「主要 API」表格新增 8 个坐席辅助端点行
  - [x] SubTask 10.2: 「核心特性」补充「坐席辅助工作台：转接后会话可被坐席接手，支持上下文延续、知识/业务辅助查询、方案沉淀回库，补齐人机协同短板」
  - [x] SubTask 10.3: 「项目结构」新增 `app/api/v1/agent.py`（schemas/agent.py 因结构中未展开 schemas 子目录，未单独列出）
  - [x] SubTask 10.4: 测试用例数更新：640+ → 668+（含技术栈表 / 测试章节 / 项目结构三处）
  - 证据：`README.md` 5 处更新全部完成

# Task Dependencies

- Task 2 / 3 依赖 Task 1（schemas 引用 session 字段，router 端点引用 session_manager 新方法）
- Task 4-7 依赖 Task 1 + Task 2 + Task 3（端点实现需要 session 方法、schemas、router 全部就绪）
- Task 4-7 之间无强依赖，**可并行**实施
- Task 8 依赖 Task 1（mark_pending 方法）与 Task 4（pending 列表端点用于验证）
- Task 9 依赖 Task 1-8 全部完成
- Task 10 依赖 Task 9（需更新测试用例数）
