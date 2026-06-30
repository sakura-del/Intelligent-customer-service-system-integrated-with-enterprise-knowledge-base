/**
 * Agent 监控面板前端逻辑。
 *
 * 负责：
 * - 拉取监控 API（overview/traces/agents/sessions）并渲染
 * - 自动刷新（每 5 秒）或手动刷新
 * - 点击 trace 行展开详情（含每步输入输出与耗时）
 * - 路由路径节点链可视化展示
 *
 * 原生 JS 实现，不引入任何框架。
 */
(function () {
    "use strict";

    // ===== 常量定义 =====
    var API_BASE = "/api/v1/monitor";
    var REFRESH_INTERVAL_MS = 5000;

    // ===== 运行时状态 =====
    var autoRefreshTimer = null;
    var currentTraceId = null;
    // 缓存最近一次 trace 列表，避免详情面板拉取后整表重渲染丢失选中态
    var lastTraces = [];

    // ===== DOM 元素引用 =====
    var overviewEls = {
        total: document.getElementById("metricTotal"),
        successRate: document.getElementById("metricSuccessRate"),
        avgDuration: document.getElementById("metricAvgDuration"),
        failed: document.getElementById("metricFailed"),
        sessions: document.getElementById("metricSessions"),
    };
    var tracesBody = document.getElementById("tracesBody");
    var tracesHint = document.getElementById("tracesHint");
    var traceDetail = document.getElementById("traceDetail");
    var agentsBody = document.getElementById("agentsBody");
    var sessionsBody = document.getElementById("sessionsBody");
    var refreshButton = document.getElementById("refreshButton");
    var autoRefreshToggle = document.getElementById("autoRefreshToggle");

    // ===== 工具函数 =====

    /**
     * HTML 转义，防止后端返回内容触发 XSS。
     */
    function escapeHtml(text) {
        if (text === null || text === undefined) return "";
        var div = document.createElement("div");
        div.textContent = String(text);
        return div.innerHTML;
    }

    /**
     * 格式化耗时（毫秒），便于阅读。
     */
    function formatDuration(ms) {
        if (ms === null || ms === undefined) return "-";
        var n = Number(ms);
        if (isNaN(n)) return "-";
        if (n < 1) return n.toFixed(2) + " ms";
        if (n < 1000) return n.toFixed(0) + " ms";
        return (n / 1000).toFixed(2) + " s";
    }

    /**
     * 格式化成功率（0-1 小数转百分比）。
     */
    function formatRate(rate) {
        if (rate === null || rate === undefined) return "-";
        return (Number(rate) * 100).toFixed(1) + "%";
    }

    /**
     * 格式化 ISO 时间为本地可读时间（仅时分秒）。
     */
    function formatTime(iso) {
        if (!iso) return "-";
        try {
            var d = new Date(iso);
            if (isNaN(d.getTime())) return iso;
            // 补零函数，避免 toLocaleTimeString 受系统设置影响
            var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
            return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
        } catch (e) {
            return iso;
        }
    }

    /**
     * 截断长文本，避免表格被撑爆。
     */
    function truncate(text, maxLen) {
        if (!text) return "";
        maxLen = maxLen || 30;
        var s = String(text);
        return s.length > maxLen ? s.slice(0, maxLen) + "..." : s;
    }

    /**
     * 调用监控 API，返回 Promise<响应 JSON>。
     * 失败时返回空数组或空对象，避免一处失败拖垮整面板。
     */
    function fetchJson(url) {
        return fetch(url, { method: "GET" })
            .then(function (resp) {
                if (!resp.ok) {
                    console.warn("监控 API 返回非 2xx：", resp.status, url);
                    return null;
                }
                return resp.json();
            })
            .catch(function (err) {
                console.warn("监控 API 请求失败：", url, err);
                return null;
            });
    }

    // ===== 渲染函数 =====

    /**
     * 渲染系统概览指标。
     */
    function renderOverview(data) {
        if (!data) return;
        overviewEls.total.textContent = data.total_traces;
        overviewEls.successRate.textContent = formatRate(data.success_rate);
        overviewEls.avgDuration.textContent = formatDuration(data.avg_duration_ms);
        overviewEls.failed.textContent = data.failed_count;
        overviewEls.sessions.textContent = data.active_sessions;
    }

    /**
     * 渲染状态标签。
     */
    function renderStatusTag(status) {
        var cls = "tag";
        var text = status || "unknown";
        if (status === "success") cls += " tag--success";
        else if (status === "failed") cls += " tag--failed";
        else if (status === "running") cls += " tag--running";
        return '<span class="' + cls + '">' + escapeHtml(text) + "</span>";
    }

    /**
     * 渲染路由路径节点链（带箭头分隔）。
     */
    function renderRoutePath(path) {
        if (!path || path.length === 0) return '<span class="empty">-</span>';
        var html = '<div class="route-path">';
        path.forEach(function (node, index) {
            if (index > 0) {
                html += '<span class="route-path__arrow">→</span>';
            }
            html += '<span class="route-path__node">' + escapeHtml(node) + "</span>";
        });
        html += "</div>";
        return html;
    }

    /**
     * 渲染 Trace 列表表格。
     */
    function renderTraces(traces) {
        lastTraces = traces || [];
        tracesHint.textContent = "共 " + lastTraces.length + " 条";

        if (!lastTraces.length) {
            tracesBody.innerHTML = '<tr><td colspan="6" class="empty">暂无 Trace 数据</td></tr>';
            return;
        }

        var html = "";
        lastTraces.forEach(function (trace) {
            var isActive = trace.trace_id === currentTraceId ? " is-active" : "";
            var escalateTag = trace.escalate_to_human
                ? '<span class="tag tag--escalate">是</span>'
                : '<span class="empty">-</span>';
            html += '<tr data-trace-id="' + escapeHtml(trace.trace_id) + '" class="' + isActive + '">'
                + "<td>" + escapeHtml(formatTime(trace.start_time)) + "</td>"
                + "<td>" + escapeHtml(trace.intent || "-") + "</td>"
                + "<td>" + renderRoutePath(trace.route_path) + "</td>"
                + "<td>" + escapeHtml(formatDuration(trace.duration_ms)) + "</td>"
                + "<td>" + renderStatusTag(trace.status) + "</td>"
                + "<td>" + escalateTag + "</td>"
                + "</tr>";
        });
        tracesBody.innerHTML = html;

        // 绑定点击事件，加载详情
        var rows = tracesBody.querySelectorAll("tr[data-trace-id]");
        rows.forEach(function (row) {
            row.addEventListener("click", function () {
                var traceId = this.getAttribute("data-trace-id");
                loadTraceDetail(traceId);
            });
        });
    }

    /**
     * 加载并渲染单条 Trace 详情。
     */
    function loadTraceDetail(traceId) {
        currentTraceId = traceId;
        // 更新列表选中态
        var rows = tracesBody.querySelectorAll("tr[data-trace-id]");
        rows.forEach(function (row) {
            if (row.getAttribute("data-trace-id") === traceId) {
                row.classList.add("is-active");
            } else {
                row.classList.remove("is-active");
            }
        });

        traceDetail.innerHTML = '<div class="empty">加载中...</div>';

        fetchJson(API_BASE + "/traces/" + encodeURIComponent(traceId)).then(function (trace) {
            if (!trace) {
                traceDetail.innerHTML = '<div class="empty">加载失败或 Trace 不存在</div>';
                return;
            }
            renderTraceDetail(trace);
        });
    }

    /**
     * 渲染 Trace 详情面板。
     */
    function renderTraceDetail(trace) {
        var metaHtml = buildTraceMetaHtml(trace);
        var stepsHtml = buildStepsHtml(trace.steps || []);
        var subTasksHtml = buildSubTasksHtml(trace.sub_tasks || []);

        traceDetail.innerHTML = metaHtml + stepsHtml + subTasksHtml;
    }

    /**
     * 构造 Trace 元信息 HTML。
     */
    function buildTraceMetaHtml(trace) {
        var rows = [
            ["Trace ID", trace.trace_id],
            ["会话 ID", trace.session_id],
            ["消息", trace.message],
            ["意图", trace.intent || "-"],
            ["开始时间", trace.start_time],
            ["结束时间", trace.end_time || "-"],
            ["耗时", formatDuration(trace.duration_ms)],
            ["轮次", trace.turn_count],
            ["失败次数", trace.failed_attempts],
            ["状态", trace.status],
            ["转人工", trace.escalate_to_human ? "是" : "否"],
            ["最终回复", trace.final_reply || "-"],
        ];
        if (trace.error) {
            rows.push(["错误", trace.error]);
        }

        var html = '<div class="trace-meta">';
        rows.forEach(function (row) {
            html += '<div class="trace-meta__row">'
                + '<span class="trace-meta__label">' + escapeHtml(row[0]) + "</span>"
                + '<span class="trace-meta__value">' + escapeHtml(row[1]) + "</span>"
                + "</div>";
        });
        html += "</div>";
        return html;
    }

    /**
     * 构造步骤列表 HTML。
     */
    function buildStepsHtml(steps) {
        if (!steps.length) return "";
        var html = '<div class="trace-steps">'
            + '<div class="trace-steps__title">执行步骤</div>';
        steps.forEach(function (step) {
            html += '<div class="step-item">'
                + '<div class="step-item__header">'
                + '<span class="step-item__node">' + escapeHtml(step.node) + "</span>"
                + '<span class="step-item__duration">' + escapeHtml(formatDuration(step.duration_ms)) + "</span>"
                + "</div>"
                + '<div class="step-item__io"><span class="step-item__io-label">输入：</span>'
                + escapeHtml(step.input_summary || "") + "</div>"
                + '<div class="step-item__io"><span class="step-item__io-label">输出：</span>'
                + escapeHtml(step.output_summary || "") + "</div>"
                + "</div>";
        });
        html += "</div>";
        return html;
    }

    /**
     * 构造子任务（agent 调用）列表 HTML。
     */
    function buildSubTasksHtml(subTasks) {
        if (!subTasks.length) return "";
        var html = '<div class="trace-steps">'
            + '<div class="trace-steps__title">Agent 调用</div>';
        subTasks.forEach(function (task) {
            var successTag = task.success
                ? '<span class="tag tag--success">成功</span>'
                : '<span class="tag tag--failed">失败</span>';
            html += '<div class="step-item">'
                + '<div class="step-item__header">'
                + '<span class="step-item__node">' + escapeHtml(task.agent_name) + "</span>"
                + '<span>' + successTag + " " + escapeHtml(formatDuration(task.duration_ms)) + "</span>"
                + "</div>"
                + '<div class="step-item__io"><span class="step-item__io-label">输入：</span>'
                + escapeHtml(task.input || "") + "</div>"
                + '<div class="step-item__io"><span class="step-item__io-label">输出：</span>'
                + escapeHtml(task.output || "") + "</div>"
                + "</div>";
        });
        html += "</div>";
        return html;
    }

    /**
     * 渲染 Agent 状态表格。
     */
    function renderAgents(agents) {
        if (!agents || !agents.length) {
            agentsBody.innerHTML = '<tr><td colspan="6" class="empty">暂无 Agent 数据</td></tr>';
            return;
        }
        var html = "";
        agents.forEach(function (agent) {
            var rateClass = agent.success_rate >= 0.8 ? "tag--success" : "tag--failed";
            html += '<tr class="is-row-hover">'
                + "<td><span class=\"route-path__node\">" + escapeHtml(agent.name) + "</span></td>"
                + "<td>" + agent.total_calls + "</td>"
                + "<td>" + agent.success_count + "</td>"
                + "<td>" + escapeHtml(formatDuration(agent.avg_duration_ms)) + "</td>"
                + "<td>" + escapeHtml(formatDuration(agent.total_duration_ms)) + "</td>"
                + '<td><span class="tag ' + rateClass + '">' + escapeHtml(formatRate(agent.success_rate)) + "</span></td>"
                + "</tr>";
        });
        agentsBody.innerHTML = html;
    }

    /**
     * 渲染活跃会话表格。
     */
    function renderSessions(sessions) {
        if (!sessions || !sessions.length) {
            sessionsBody.innerHTML = '<tr><td colspan="6" class="empty">暂无活跃会话</td></tr>';
            return;
        }
        var html = "";
        sessions.forEach(function (session) {
            html += '<tr class="is-row-hover">'
                + "<td><code>" + escapeHtml(truncate(session.session_id, 12)) + "</code></td>"
                + "<td>" + escapeHtml(session.channel || "-") + "</td>"
                + "<td>" + session.turn_count + "</td>"
                + "<td>" + session.failed_attempts + "</td>"
                + "<td>" + escapeHtml(session.current_intent || "-") + "</td>"
                + "<td>" + escapeHtml(formatTime(session.last_active_at)) + "</td>"
                + "</tr>";
        });
        sessionsBody.innerHTML = html;
    }

    // ===== 数据拉取 =====

    /**
     * 并行拉取所有监控数据并渲染。
     * 各接口独立失败不影响其他模块展示。
     */
    function refreshAll() {
        // 概览、agents、sessions 并行；traces 单独拉取以便复用 loadTraceDetail
        Promise.all([
            fetchJson(API_BASE + "/overview"),
            fetchJson(API_BASE + "/traces?limit=50"),
            fetchJson(API_BASE + "/agents"),
            fetchJson(API_BASE + "/sessions"),
        ]).then(function (results) {
            renderOverview(results[0]);
            renderTraces(results[1]);
            renderAgents(results[2]);
            renderSessions(results[3]);

            // 若当前选中 trace，刷新其详情
            if (currentTraceId) {
                loadTraceDetail(currentTraceId);
            }
        });
    }

    // ===== 自动刷新控制 =====

    function startAutoRefresh() {
        stopAutoRefresh();
        autoRefreshTimer = setInterval(refreshAll, REFRESH_INTERVAL_MS);
    }

    function stopAutoRefresh() {
        if (autoRefreshTimer) {
            clearInterval(autoRefreshTimer);
            autoRefreshTimer = null;
        }
    }

    // ===== 事件绑定 =====

    refreshButton.addEventListener("click", refreshAll);

    autoRefreshToggle.addEventListener("change", function () {
        if (this.checked) {
            startAutoRefresh();
        } else {
            stopAutoRefresh();
        }
    });

    // 页面隐藏时暂停刷新，节省资源
    document.addEventListener("visibilitychange", function () {
        if (document.hidden) {
            stopAutoRefresh();
        } else if (autoRefreshToggle.checked) {
            startAutoRefresh();
            // 切回时立即刷新一次，避免数据陈旧
            refreshAll();
        }
    });

    // ===== 初始化 =====
    refreshAll();
    if (autoRefreshToggle.checked) {
        startAutoRefresh();
    }
})();
