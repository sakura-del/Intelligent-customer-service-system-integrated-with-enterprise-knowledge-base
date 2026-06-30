/**
 * 运营数据看板前端逻辑。
 *
 * 负责：
 * - 拉取 dashboard / release-checklist / experiments API 并渲染
 * - 自动刷新（每 30 秒）或手动刷新
 * - 横向条形图（纯 CSS）展示分类分布
 * - 创建实验对话框
 *
 * 原生 JS 实现，不引入任何框架与图表库。
 */
(function () {
    "use strict";

    // ===== 常量定义 =====
    var API_BASE = "/api/v1/operations";
    var REFRESH_INTERVAL_MS = 30000;

    // ===== 运行时状态 =====
    var autoRefreshTimer = null;

    // ===== DOM 元素引用 =====
    var els = {
        sessionTotal: document.getElementById("sessionTotal"),
        sessionActive: document.getElementById("sessionActive"),
        sessionAvgTurn: document.getElementById("sessionAvgTurn"),
        ticketTotal: document.getElementById("ticketTotal"),
        ticketNew: document.getElementById("ticketNew"),
        ticketResolved: document.getElementById("ticketResolved"),
        ticketUnresolved: document.getElementById("ticketUnresolved"),
        escalationTotal: document.getElementById("escalationTotal"),
        escalationPickup: document.getElementById("escalationPickup"),
        satisfactionScore: document.getElementById("satisfactionScore"),
        satisfactionSample: document.getElementById("satisfactionSample"),
        satisfactionPositive: document.getElementById("satisfactionPositive"),
        knowledgeTotal: document.getElementById("knowledgeTotal"),
        knowledgeRecent: document.getElementById("knowledgeRecent"),
        ticketCategoryBody: document.getElementById("ticketCategoryBody"),
        escalationReasonBody: document.getElementById("escalationReasonBody"),
        knowledgeTypeBody: document.getElementById("knowledgeTypeBody"),
        checklistBody: document.getElementById("checklistBody"),
        checklistPassed: document.getElementById("checklistPassed"),
        checklistFailed: document.getElementById("checklistFailed"),
        checklistWarned: document.getElementById("checklistWarned"),
        experimentsBody: document.getElementById("experimentsBody"),
        refreshButton: document.getElementById("refreshButton"),
        autoRefreshToggle: document.getElementById("autoRefreshToggle"),
        createExperimentBtn: document.getElementById("createExperimentBtn"),
        createDialog: document.getElementById("createDialog"),
        dialogCloseBtn: document.getElementById("dialogCloseBtn"),
        dialogCancelBtn: document.getElementById("dialogCancelBtn"),
        dialogSubmitBtn: document.getElementById("dialogSubmitBtn"),
        expName: document.getElementById("expName"),
        expDesc: document.getElementById("expDesc"),
        expVariants: document.getElementById("expVariants"),
        expMetrics: document.getElementById("expMetrics"),
    };

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
     * 调用 API，返回 Promise<响应 JSON>。
     * 失败时返回 null 并打印日志。
     */
    function fetchJson(url, options) {
        return fetch(url, options)
            .then(function (resp) {
                if (!resp.ok) {
                    return resp.text().then(function (body) {
                        throw new Error("HTTP " + resp.status + "：" + body);
                    });
                }
                if (resp.status === 204) return null;
                return resp.json();
            })
            .catch(function (err) {
                console.error("[operations] 请求失败：" + url, err);
                return null;
            });
    }

    /**
     * 格式化数值：保留指定位数小数。
     */
    function formatNumber(value, digits) {
        digits = digits || 0;
        var n = Number(value);
        if (isNaN(n)) return "-";
        return n.toFixed(digits);
    }

    /**
     * 格式化百分比（0-1 小数转百分比）。
     */
    function formatPercent(value) {
        if (value === null || value === undefined) return "-";
        var n = Number(value);
        if (isNaN(n)) return "-";
        return (n * 100).toFixed(1) + "%";
    }

    /**
     * 格式化 ISO 时间为本地可读时间。
     */
    function formatTime(iso) {
        if (!iso) return "-";
        try {
            var d = new Date(iso);
            if (isNaN(d.getTime())) return iso;
            var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
            return (
                d.getFullYear() + "-" +
                pad(d.getMonth() + 1) + "-" +
                pad(d.getDate()) + " " +
                pad(d.getHours()) + ":" +
                pad(d.getMinutes()) + ":" +
                pad(d.getSeconds())
            );
        } catch (e) {
            return iso;
        }
    }

    // ===== 渲染：看板数据 =====

    function renderDashboard(data) {
        if (!data) return;

        var session = data.session || {};
        var ticket = data.ticket || {};
        var escalation = data.escalation || {};
        var satisfaction = data.satisfaction || {};
        var knowledge = data.knowledge || {};

        if (els.sessionTotal) els.sessionTotal.textContent = session.total_sessions || 0;
        if (els.sessionActive) els.sessionActive.textContent = session.active_sessions || 0;
        if (els.sessionAvgTurn) els.sessionAvgTurn.textContent = formatNumber(session.avg_turn_count, 2);

        if (els.ticketTotal) els.ticketTotal.textContent = ticket.total || 0;
        if (els.ticketNew) els.ticketNew.textContent = ticket.new_count || 0;
        if (els.ticketResolved) els.ticketResolved.textContent = ticket.resolved_count || 0;
        if (els.ticketUnresolved) els.ticketUnresolved.textContent = ticket.unresolved_count || 0;

        if (els.escalationTotal) els.escalationTotal.textContent = escalation.total_escalations || 0;
        if (els.escalationPickup) els.escalationPickup.textContent = formatPercent(escalation.human_pickup_rate);

        if (els.satisfactionScore) els.satisfactionScore.textContent = formatNumber(satisfaction.avg_score, 2);
        if (els.satisfactionSample) els.satisfactionSample.textContent = satisfaction.sample_count || 0;
        if (els.satisfactionPositive) els.satisfactionPositive.textContent = formatPercent(satisfaction.positive_rate);

        if (els.knowledgeTotal) els.knowledgeTotal.textContent = knowledge.total_entries || 0;
        if (els.knowledgeRecent) els.knowledgeRecent.textContent = knowledge.recent_7d_ingest_count || 0;

        renderBarList(els.ticketCategoryBody, ticket.category_distribution, "success");
        renderBarList(els.escalationReasonBody, escalation.reason_distribution, "warning");
        renderBarList(els.knowledgeTypeBody, knowledge.type_distribution, "");
    }

    /**
     * 渲染横向条形图列表（纯 CSS）。
     * distribution: {key: value}
     */
    function renderBarList(container, distribution, fillClass) {
        if (!container) return;
        if (!distribution || Object.keys(distribution).length === 0) {
            container.innerHTML = '<div class="empty">暂无数据</div>';
            return;
        }
        var entries = Object.keys(distribution).map(function (key) {
            return { key: key, value: distribution[key] };
        });
        // 按值倒序
        entries.sort(function (a, b) { return b.value - a.value; });
        var max = entries.reduce(function (m, e) { return Math.max(m, e.value); }, 0);
        if (max <= 0) max = 1;

        var html = '<div class="bar-list">';
        entries.forEach(function (entry) {
            var ratio = (entry.value / max) * 100;
            var cls = "bar-item__fill";
            if (fillClass) cls += " bar-item__fill--" + fillClass;
            html +=
                '<div class="bar-item">' +
                '<div class="bar-item__head"><span>' + escapeHtml(entry.key) + '</span><span>' + entry.value + '</span></div>' +
                '<div class="bar-item__track"><div class="' + cls + '" style="width:' + ratio.toFixed(1) + '%"></div></div>' +
                '</div>';
        });
        html += '</div>';
        container.innerHTML = html;
    }

    // ===== 渲染：上线检查清单 =====

    function renderChecklist(report) {
        if (!report) return;
        if (els.checklistPassed) els.checklistPassed.textContent = "通过 " + report.passed;
        if (els.checklistFailed) els.checklistFailed.textContent = "失败 " + report.failed;
        if (els.checklistWarned) els.checklistWarned.textContent = "警告 " + report.warned;

        if (!els.checklistBody) return;
        var items = report.items || [];
        if (items.length === 0) {
            els.checklistBody.innerHTML = '<tr><td colspan="4" class="empty">暂无检查结果</td></tr>';
            return;
        }
        var html = "";
        items.forEach(function (item) {
            var tagClass = "tag--info";
            var tagText = item.status;
            if (item.status === "pass") { tagClass = "tag--success"; tagText = "通过"; }
            else if (item.status === "fail") { tagClass = "tag--failed"; tagText = "失败"; }
            else if (item.status === "warn") { tagClass = "tag--warning"; tagText = "警告"; }
            html +=
                '<tr>' +
                '<td>' + escapeHtml(item.name) + '</td>' +
                '<td><span class="tag ' + tagClass + '">' + tagText + '</span></td>' +
                '<td>' + escapeHtml(item.message) + '</td>' +
                '<td>' + formatNumber(item.duration_ms, 2) + ' ms</td>' +
                '</tr>';
        });
        els.checklistBody.innerHTML = html;
    }

    // ===== 渲染：实验列表 =====

    function renderExperiments(experiments) {
        if (!els.experimentsBody) return;
        if (!experiments || experiments.length === 0) {
            els.experimentsBody.innerHTML = '<tr><td colspan="6" class="empty">暂无实验</td></tr>';
            return;
        }
        var html = "";
        experiments.forEach(function (exp) {
            var variants = (exp.variants || []).map(function (v) {
                return escapeHtml(v.name) + ':' + v.weight;
            }).join(' / ');
            var metrics = (exp.target_metrics || []).join(', ');
            var statusHtml = exp.enabled
                ? '<span class="tag tag--success">启用</span>'
                : '<span class="tag tag--info">已停用</span>';
            html +=
                '<tr>' +
                '<td>' + escapeHtml(exp.name) + '</td>' +
                '<td>' + escapeHtml(exp.description) + '</td>' +
                '<td>' + variants + '</td>' +
                '<td>' + escapeHtml(metrics) + '</td>' +
                '<td>' + statusHtml + '</td>' +
                '<td>' + formatTime(exp.updated_at) + '</td>' +
                '</tr>';
        });
        els.experimentsBody.innerHTML = html;
    }

    // ===== 数据拉取 =====

    function loadDashboard() {
        return fetchJson(API_BASE + "/dashboard").then(renderDashboard);
    }

    function loadChecklist() {
        return fetchJson(API_BASE + "/release-checklist").then(renderChecklist);
    }

    function loadExperiments() {
        return fetchJson(API_BASE + "/experiments").then(renderExperiments);
    }

    function refreshAll() {
        return Promise.all([loadDashboard(), loadChecklist(), loadExperiments()]);
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

    // ===== 创建实验对话框 =====

    function openCreateDialog() {
        if (els.createDialog) {
            els.createDialog.style.display = "flex";
            if (els.expName) els.expName.value = "";
            if (els.expDesc) els.expDesc.value = "";
            if (els.expVariants) els.expVariants.value = "control:9\ntreatment:1";
            if (els.expMetrics) els.expMetrics.value = "";
        }
    }

    function closeCreateDialog() {
        if (els.createDialog) els.createDialog.style.display = "none";
    }

    /**
     * 解析分组配置文本，每行 "name:weight"。
     */
    function parseVariants(text) {
        var variants = [];
        var lines = (text || "").split(/\r?\n/);
        lines.forEach(function (line) {
            var trimmed = line.trim();
            if (!trimmed) return;
            var parts = trimmed.split(":");
            if (parts.length < 1) return;
            var name = parts[0].trim();
            if (!name) return;
            var weight = 1;
            if (parts.length >= 2) {
                var w = parseInt(parts[1].trim(), 10);
                if (!isNaN(w) && w >= 0) weight = w;
            }
            variants.push({ name: name, weight: weight, description: "" });
        });
        return variants;
    }

    function submitCreateExperiment() {
        var name = els.expName ? els.expName.value.trim() : "";
        if (!name) {
            alert("请填写实验名");
            return;
        }
        var variants = parseVariants(els.expVariants ? els.expVariants.value : "");
        if (variants.length === 0) {
            alert("请至少配置一个分组");
            return;
        }
        var metrics = els.expMetrics ? els.expMetrics.value.trim() : "";
        var targetMetrics = metrics
            ? metrics.split(",").map(function (s) { return s.trim(); }).filter(Boolean)
            : [];
        var payload = {
            name: name,
            description: els.expDesc ? els.expDesc.value.trim() : "",
            variants: variants,
            target_metrics: targetMetrics,
            enabled: true,
        };
        fetchJson(API_BASE + "/experiments", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        }).then(function (data) {
            if (data) {
                closeCreateDialog();
                loadExperiments();
            } else {
                alert("创建实验失败，请查看控制台日志");
            }
        });
    }

    // ===== 事件绑定 =====

    function bindEvents() {
        if (els.refreshButton) {
            els.refreshButton.addEventListener("click", refreshAll);
        }
        if (els.autoRefreshToggle) {
            els.autoRefreshToggle.addEventListener("change", function () {
                if (els.autoRefreshToggle.checked) {
                    startAutoRefresh();
                } else {
                    stopAutoRefresh();
                }
            });
        }
        if (els.createExperimentBtn) {
            els.createExperimentBtn.addEventListener("click", openCreateDialog);
        }
        if (els.dialogCloseBtn) {
            els.dialogCloseBtn.addEventListener("click", closeCreateDialog);
        }
        if (els.dialogCancelBtn) {
            els.dialogCancelBtn.addEventListener("click", closeCreateDialog);
        }
        if (els.dialogSubmitBtn) {
            els.dialogSubmitBtn.addEventListener("click", submitCreateExperiment);
        }
    }

    // ===== 初始化 =====

    function init() {
        bindEvents();
        refreshAll();
        // 默认开启自动刷新
        if (els.autoRefreshToggle && els.autoRefreshToggle.checked) {
            startAutoRefresh();
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
