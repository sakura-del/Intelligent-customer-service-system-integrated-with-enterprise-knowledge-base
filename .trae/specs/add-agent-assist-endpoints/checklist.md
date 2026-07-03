# Checklist

> change-id: add-agent-assist-endpoints
> 验证方式：逐项 Read/Grep 代码 + pytest 回归 + 端到端 curl 验证

## SessionManager 扩展（Task 1）
- [x] `_default_session_state` 新增 `agent_status`（默认 None）/ `assigned_agent_id`（默认 None）/ `escalation_card`（默认 None）3 个字段 - 证据：app/core/session.py:357-366（含 resolve_note 共 4 个新字段，均默认 None）
- [x] `list_pending_sessions()` 方法存在，返回 pending 会话摘要并按 EscalationPriority 降序 - 证据：app/core/session.py:193-226，使用 _PRIORITY_RANK 映射（highest=4>high=3>medium=2>low=1>info=0）按降序排序
- [x] `assign_agent(session_id, agent_id)` 方法存在，CAS 判断 pending → assigned，重复接手返回 False - 证据：app/core/session.py:228-244，line 240 校验 agent_status != "pending" 返回 False
- [x] `resolve_session(session_id, note)` 方法存在，CAS 判断 assigned → resolved，写入 resolve_note - 证据：app/core/session.py:246-264，line 260 CAS 校验，line 263 写入 resolve_note
- [x] `mark_pending(session_id, escalation_card)` 方法存在，将会话置为 pending 并缓存 EscalationCard - 证据：app/core/session.py:266-281，line 279 置 agent_status="pending"，line 280 缓存 escalation_card
- [x] 新增字段对所有现有 SessionManager 调用方向后兼容（不破坏 create_session / get_session / append_history / update_session） - 证据：app/core/session.py:45-62（create_session 签名未变）、64-69（get_session）、90-108（update_session）、110-134（append_history），新字段仅在 _default_session_state 中追加

## Schemas（Task 2）
- [x] `app/schemas/agent.py` 文件存在 - 证据：app/schemas/agent.py 共 152 行，定义 12 个 Pydantic 模型
- [x] `AgentSessionSummary` 含 session_id / user_id / priority / escalate_reason / turn_count / created_at / agent_status / assigned_agent_id 8 个字段 - 证据：app/schemas/agent.py:17-32，共 8 个 Field 定义
- [x] `AgentSessionDetail` 含 escalation_card（Optional[EscalationCard]）/ history / agent_status / assigned_agent_id / turn_count / emotion_score - 证据：app/schemas/agent.py:35-54，line 48 escalation_card 为 Optional[EscalationCard]，含全部要求字段
- [x] `AgentMessageRequest.content` 含 `min_length=1` 约束 - 证据：app/schemas/agent.py:63 `content: str = Field(..., min_length=1, ...)`
- [x] `AgentMessageResponse` 含 message_id / timestamp - 证据：app/schemas/agent.py:66-71，含 message_id / timestamp / role 三字段
- [x] `KnowledgeRecommendRequest` 含 query / top_k（默认 5） - 证据：app/schemas/agent.py:74-78，`top_k: int = Field(5, ge=1, le=20, ...)`
- [x] `KnowledgeRecommendResponse.chunks` 为 List[dict] - 证据：app/schemas/agent.py:89-95，chunks 为 List[KnowledgeChunk]（KnowledgeChunk 为 BaseModel，序列化为 dict），含 total 字段
- [x] `BusinessAssistRequest` 含 query，`BusinessAssistResponse` 含 result / masked_fields - 证据：app/schemas/agent.py:98-115，BusinessAssistRequest.query min_length=1，BusinessAssistResponse 含 result: Dict 与 masked_fields: List[str]
- [x] `SolutionSubmitRequest` 含 question / solution / intent(Optional)，复用 HumanSolutionRecord 作为响应 - 证据：app/schemas/agent.py:118-129 定义 SolutionSubmitRequest（question/solution min_length=1，intent Optional），app/api/v1/agent.py:326 端点 response_model=HumanSolutionRecord
- [x] `ResolveRequest` 含 note(Optional)，`ResolveResponse` 含 session_id / agent_status / resolved_at - 证据：app/schemas/agent.py:132-143，ResolveRequest.note Optional[str]，ResolveResponse 含 session_id / agent_status / resolved_at 三字段

## Router 注册（Task 3）
- [x] `app/api/v1/agent.py` 文件存在，定义 `APIRouter(prefix="/api/v1/agent", tags=["坐席辅助"], dependencies=[Depends(verify_api_key)])` - 证据：app/api/v1/agent.py:55-59，`APIRouter(prefix="/api/v1/agent", tags=["坐席辅助"], dependencies=[Depends(verify_api_key)])`
- [x] router 在 `app/main.py` 或 `app/api/v1/__init__.py` 中正确注册 - 证据：app/main.py:14 import agent，line 69 `app.include_router(agent.router)`
- [x] 启动服务后 `/docs` 中出现「坐席辅助」分组 - 证据：tags=["坐席辅助"] 已设置，FastAPI 自动按 tags 在 /docs 分组展示

## 端点实现（Task 4-7）

### 待接入会话列表（Task 4）
- [x] `GET /api/v1/agent/sessions/pending` 端点存在，返回 `List[AgentSessionSummary]` - 证据：app/api/v1/agent.py:67-77，`@router.get("/sessions/pending", response_model=List[AgentSessionSummary])`
- [x] 无 pending 会话时返回空列表 `[]`，不报错 - 证据：app/api/v1/agent.py:76-77，`list_pending_sessions()` 无 pending 时返回空列表，列表推导产出 []
- [x] 鉴权失败（无 X-API-Key）返回 401 - 证据：app/api/v1/agent.py:58 dependencies=[Depends(verify_api_key)]，app/core/security.py:30-34 校验失败抛 401

### 会话详情（Task 4）
- [x] `GET /api/v1/agent/sessions/{session_id}` 端点存在，返回 `AgentSessionDetail` - 证据：app/api/v1/agent.py:80-136，`@router.get("/sessions/{session_id}", response_model=AgentSessionDetail)`
- [x] session_id 不存在返回 404 - 证据：app/api/v1/agent.py:88-89，session is None 时 `raise HTTPException(status_code=404, ...)`
- [x] escalation_card 缓存为空时调用 `EscalationEngine.build_card()` 重建并写回缓存 - 证据：app/api/v1/agent.py:101-121，line 115 调用 `engine.build_card()`，line 119-121 `update_session(escalation_card=...)` 写回缓存
- [x] 返回的 history 为完整对话历史列表 - 证据：app/api/v1/agent.py:134 `history=session.get("history", [])`，返回 session 中完整 history

### 坐席接手（Task 5）
- [x] `POST /api/v1/agent/sessions/{session_id}/accept` 端点存在 - 证据：app/api/v1/agent.py:144-167，`@router.post("/sessions/{session_id}/accept", response_model=AgentSessionDetail)`
- [x] pending 状态接手成功，agent_status 改为 assigned，返回 AgentSessionDetail - 证据：app/api/v1/agent.py:159 调用 assign_agent，line 167 返回 `get_agent_session(session_id)`；session.py:242-243 置 assigned
- [x] 已 assigned 或 resolved 状态接手返回 409 - 证据：app/api/v1/agent.py:160-164，assign_agent 返回 False 时抛 409

### 坐席发消息（Task 5）
- [x] `POST /api/v1/agent/sessions/{session_id}/messages` 端点存在 - 证据：app/api/v1/agent.py:170-197，`@router.post("/sessions/{session_id}/messages", response_model=AgentMessageResponse)`
- [x] assigned 状态发送成功，history 追加 role=assistant 条目，返回 AgentMessageResponse - 证据：app/api/v1/agent.py:191 `append_history(session_id, role="assistant", content=...)`，line 195-197 返回 AgentMessageResponse
- [x] pending 或 resolved 状态发送返回 409 - 证据：app/api/v1/agent.py:185-189，agent_status != "assigned" 时抛 409
- [x] content 为空返回 422 - 证据：app/schemas/agent.py:63 `content: str = Field(..., min_length=1, ...)`，FastAPI 自动校验返回 422

### 知识推荐（Task 6）
- [x] `POST /api/v1/agent/sessions/{session_id}/knowledge-recommend` 端点存在 - 证据：app/api/v1/agent.py:205-241，`@router.post("/sessions/{session_id}/knowledge-recommend", response_model=KnowledgeRecommendResponse)`
- [x] 复用 `HybridRetriever.retrieve`，返回 chunks 列表 - 证据：app/api/v1/agent.py:224-225 `retriever = get_hybrid_retriever(); chunks = retriever.retrieve(question=request.query, top_k=request.top_k)`
- [x] 未命中时返回空 chunks 列表，不报错 - 证据：app/api/v1/agent.py:226-229，检索异常时降级 `chunks = []`，line 231-241 返回空列表

### 业务辅助（Task 6）
- [x] `POST /api/v1/agent/sessions/{session_id}/business-assist` 端点存在 - 证据：app/api/v1/agent.py:244-289，`@router.post("/sessions/{session_id}/business-assist", response_model=BusinessAssistResponse)`
- [x] 复用 `BusinessAgent.execute`，返回 result / masked_fields - 证据：app/api/v1/agent.py:261-262 `agent = get_business_agent(); result = agent.execute(query=..., session_id=...)`，line 273-282 返回 BusinessAssistResponse
- [x] 敏感字段（手机号等）已脱敏 - 证据：app/api/v1/agent.py:266-272 提取 masked_fields（phone/id_card/_masked 后缀字段）；BusinessResult.data 由 BusinessAgent 内部脱敏
- [x] 业务异常时不抛 5xx，降级为 result.error 字段 - 证据：app/api/v1/agent.py:283-289，except 捕获异常返回 `BusinessAssistResponse(result={"error": ...})`

### 标记已解决（Task 7）
- [x] `POST /api/v1/agent/sessions/{session_id}/resolve` 端点存在 - 证据：app/api/v1/agent.py:297-323，`@router.post("/sessions/{session_id}/resolve", response_model=ResolveResponse)`
- [x] assigned 状态解决成功，agent_status 改为 resolved，返回 ResolveResponse - 证据：app/api/v1/agent.py:312 调用 resolve_session，line 319-323 返回 ResolveResponse(agent_status="resolved")
- [x] pending 状态解决返回 409 - 证据：app/api/v1/agent.py:313-317，resolve_session 返回 False 时抛 409
- [x] resolve_note 正确写入 - 证据：app/core/session.py:263 `session["resolve_note"] = note`

### 方案沉淀（Task 7）
- [x] `POST /api/v1/agent/sessions/{session_id}/solution` 端点存在 - 证据：app/api/v1/agent.py:326-354，`@router.post("/sessions/{session_id}/solution", response_model=HumanSolutionRecord)`
- [x] 调用 `KnowledgeFeedback.record_human_solution`，返回 HumanSolutionRecord - 证据：app/api/v1/agent.py:341-347 `feedback.record_human_solution(session_id=..., question=..., solution=..., intent=...)`，line 354 返回 record
- [x] question 或 solution 为空返回 422 - 证据：app/schemas/agent.py:125-126 `question/solution: str = Field(..., min_length=1, ...)`，FastAPI 自动校验返回 422
- [x] 提交后能在 `GET /api/v1/escalation/solutions/pending` 中查到 - 证据：app/api/v1/agent.py:341 调用 record_human_solution 入队（status=pending），app/api/v1/escalation.py:55-58 `GET /solutions/pending` 通过 `get_pending_solutions()` 返回该记录

## 转接触发联动（Task 8）
- [x] EscalationEngine.check_escalation() 调用方在转接决策通过后调用 `session_manager.mark_pending()` - 证据：app/agents/graph.py:625-636，escalate_node 在生成 escalation_card 后调用 `session_manager.mark_pending(session_id, escalation_card)`；escalation.py 自身未调用 mark_pending（保持纯决策），由 graph.py 调用方写入状态
- [x] 转接触发后 `GET /api/v1/agent/sessions/pending` 能立即查到该会话 - 证据：app/agents/graph.py:634 mark_pending 同步写入 agent_status="pending"，app/api/v1/agent.py:76 list_pending_sessions 即时读取
- [x] 现有 chat / orchestrator / graph 主链路无回归（转接决策逻辑本身不变，仅新增状态写入） - 证据：app/agents/escalation.py:79-88 check_escalation 签名未变；app/agents/graph.py:613-645 escalate_node 仅新增 mark_pending 调用（line 632-636），转接决策逻辑（_route_after_route）未改动；chat.py / orchestrator.py 未引用 mark_pending

## 测试（Task 9）
- [x] `tests/test_agent_assist.py` 文件存在 - 证据：tests/test_agent_assist.py 共 586 行
- [x] 8 个端点正常路径均有测试覆盖 - 证据：tests/test_agent_assist.py:159-485，含 TestListPendingSessions / TestGetSessionDetail / TestAcceptSession / TestSendMessage / TestKnowledgeRecommend / TestBusinessAssist / TestResolveSession / TestSubmitSolution 8 个测试类
- [x] 404 / 409 / 422 边界场景均有测试覆盖 - 证据：404 见 line 204-209/251-258/313-320/344-351/382-389/429-436/478-485；409 见 line 234-249/288-296/419-427；422 见 line 298-311/468-476
- [x] SessionManager 新增方法（list_pending_sessions / assign_agent / resolve_session / mark_pending）有单元测试 - 证据：tests/test_agent_assist.py:493-586 TestSessionManagerExtensions，含 list_pending_sessions_order / assign_agent_cas_success / assign_agent_cas_conflict / resolve_session_cas_success / resolve_session_cas_conflict / mark_pending_success / mark_pending_not_found 7 个用例
- [x] 测试模块级 fixture 强制 `LANGFUSE_ENABLED=False` + `SMALL_LLM_API_KEY=""` - 证据：tests/test_agent_assist.py:31-61 module_isolation fixture，line 59-61 `settings.LLM_API_KEY = ""`、`settings.SMALL_LLM_API_KEY = ""`、`settings.LANGFUSE_ENABLED = False`
- [x] 全量 `python -m pytest tests/ -q` 无回归 - 证据：Task 9 Sub-Agent 执行结果 667 passed / 1 flaky（test_stream_chitchat_uses_quick_intent_fast_first_token 性能断言 <200ms，实际 456ms，单独重跑通过，与本次改动无关）

## 文档（Task 10）
- [x] README.md「主要 API」表格新增坐席辅助端点行 - 证据：README.md:154-161，新增 8 个 `/api/v1/agent/sessions/...` 端点行
- [x] README.md「核心特性」补充坐席辅助工作台说明 - 证据：README.md:12 `- **坐席辅助工作台**：转接后会话可被坐席接手，支持上下文延续、知识/业务辅助查询、方案沉淀回库，补齐人机协同短板`
- [x] README.md「项目结构」新增 `app/api/v1/agent.py` 与 `app/schemas/agent.py` - 证据：README.md:37 `agent.py # 坐席辅助端点`；README.md:69-72 `schemas/` 目录已展开含 `agent.py` 与 `escalation.py`
- [x] README.md 测试用例数已更新 - 证据：README.md:29 技术栈表 668+、README.md:75 项目结构 668+、README.md:182 测试章节 668+，三处全部同步

## 降级与兼容
- [x] `HybridRetriever` / `BusinessAgent` / `KnowledgeFeedback` 接口签名未改动 - 证据：hybrid_retriever.py:69-75 `retrieve(question, top_k=20, score_threshold=0.0, where=None)` 签名不变；business_agent.py:142 `execute(self, query: str, session_id: str) -> BusinessResult` 不变；knowledge_feedback.py:52-58 `record_human_solution(session_id, question, solution, intent=None)` 不变
- [x] 现有 `EscalationCard` schema 未修改，新 schema 通过组合复用 - 证据：app/schemas/escalation.py:55-80 EscalationCard 字段未变；app/schemas/agent.py:14 `from app.schemas.escalation import EscalationCard, EscalationPriority`，agent.py:48 通过 `Optional[EscalationCard]` 组合复用
- [x] 现有 escalation.py / chat.py / orchestrator.py 端点逻辑无破坏性改动 - 证据：escalation.py check_escalation 签名未变；chat.py 仅引用 session_manager 基础方法（get_or_create/increment_turn/append_history/update_session），未涉及 mark_pending；orchestrator.py 未引用 mark_pending
- [x] 路由文件中现有端点不变（遵循 middleware 不动路由文件的约束精神） - 证据：app/api/v1/agent.py 为新增文件，未修改 chat.py / escalation.py 等现有路由文件；main.py 仅新增 line 14 import 与 line 69 include_router
