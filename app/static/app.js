/**
 * 智能客服系统前端逻辑。
 *
 * 负责：
 * - 会话保持（首次不带 session_id，后续复用后端返回的 session_id）
 * - 渠道选择与 API Key 持久化（localStorage）
 * - 消息渲染（用户右侧、机器人左侧、来源可折叠）
 * - 加载状态与错误处理
 *
 * 原生 JS 实现，不引入任何框架。
 */
(function () {
    "use strict";

    // ===== 常量定义 =====
    var CHAT_API_URL = "/api/v1/chat";
    var CHAT_STREAM_API_URL = "/api/v1/chat/stream";   // SSE 流式端点
    var API_KEY_STORAGE = "cs_api_key";
    var STREAM_MODE_STORAGE = "cs_stream_mode";        // 流式模式偏好持久化键
    var WELCOME_TEXT = "您好，我是智能客服助手，请问有什么可以帮您？";
    var SSE_EVENT_SEPARATOR = "\n\n";                   // SSE 事件之间的分隔符

    // ===== 运行时状态 =====
    var sessionId = null;       // 会话 ID，首次对话后由后端返回
    var isSending = false;      // 防止重复发送
    var streamMode = true;      // 流式模式开关，默认开启，可由用户切换

    // ===== DOM 元素引用 =====
    var messageList = document.getElementById("messageList");
    var messageInput = document.getElementById("messageInput");
    var sendButton = document.getElementById("sendButton");
    var channelSelect = document.getElementById("channelSelect");
    var apiKeyInput = document.getElementById("apiKeyInput");
    var streamToggle = document.getElementById("streamToggle");

    // ===== 工具函数 =====

    /**
     * HTML 转义，防止用户输入与后端返回内容触发 XSS。
     */
    function escapeHtml(text) {
        if (text === null || text === undefined) return "";
        var div = document.createElement("div");
        div.textContent = String(text);
        return div.innerHTML;
    }

    /**
     * 轻量 Markdown 渲染：用正则实现最小子集，避免引入新依赖。
     * 支持：代码块、行内代码、加粗、列表（无序/有序）、换行保留。
     * XSS 防护：所有文本先经 HTML 实体转义，代码块内容强制转义。
     * @param {string} text - 原始 markdown 文本
     * @returns {string} 渲染后的 HTML 字符串
     */
    function renderMarkdown(text) {
        if (!text) return "";

        var parts = [];
        // 代码块正则：```lang\n...```，全局匹配
        var codeBlockRegex = /```(\w*)\n?([\s\S]*?)```/g;
        var lastIndex = 0;
        var match;

        while ((match = codeBlockRegex.exec(text)) !== null) {
            // 代码块之前的普通文本走行内渲染
            if (match.index > lastIndex) {
                parts.push(renderInlineMarkdown(text.slice(lastIndex, match.index)));
            }
            // 代码块内容强制转义，<pre> 标签保留换行
            var langAttr = match[1] ? ' class="language-' + escapeHtml(match[1]) + '"' : "";
            var codeContent = escapeHtml(match[2].replace(/\n$/, ""));
            parts.push('<pre><code' + langAttr + '>' + codeContent + '</code></pre>');
            lastIndex = codeBlockRegex.lastIndex;
        }
        // 末尾剩余普通文本
        if (lastIndex < text.length) {
            parts.push(renderInlineMarkdown(text.slice(lastIndex)));
        }
        return parts.join("");
    }

    /**
     * 行内 Markdown 渲染：处理行内代码、加粗、列表、换行。
     * 输入文本先整体转义，再按规则替换为标签，确保 XSS 安全。
     * @param {string} text - 原始文本片段
     * @returns {string} 行内渲染后的 HTML 字符串
     */
    function renderInlineMarkdown(text) {
        if (!text) return "";

        // 先转义 HTML 实体（防 XSS）
        text = escapeHtml(text);

        // 行内代码：用占位符替换，避免内部内容被其他规则二次处理
        var codeSpans = [];
        text = text.replace(/`([^`]+)`/g, function (m, code) {
            codeSpans.push('<code>' + code + '</code>');
            return '\u0000CODE' + (codeSpans.length - 1) + '\u0000';
        });

        // 加粗 **...**
        text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

        // 按行处理列表与换行
        var lines = text.split('\n');
        var html = [];
        var inList = false;
        var listType = null;

        // 关闭当前列表（类型切换或遇到非列表行时调用）
        function closeList() {
            if (inList) {
                html.push('</' + listType + '>');
                inList = false;
                listType = null;
            }
        }

        for (var i = 0; i < lines.length; i++) {
            var line = lines[i];
            var ulMatch = line.match(/^\s*[-*]\s+(.*)$/);
            var olMatch = line.match(/^\s*\d+\.\s+(.*)$/);

            if (ulMatch) {
                if (!inList || listType !== 'ul') {
                    closeList();
                    html.push('<ul>');
                    inList = true;
                    listType = 'ul';
                }
                html.push('<li>' + ulMatch[1] + '</li>');
            } else if (olMatch) {
                if (!inList || listType !== 'ol') {
                    closeList();
                    html.push('<ol>');
                    inList = true;
                    listType = 'ol';
                }
                html.push('<li>' + olMatch[1] + '</li>');
            } else {
                closeList();
                if (line.trim() === '') continue;  // 空行跳过，避免多余 <br>
                html.push(line + '<br>');
            }
        }
        closeList();

        text = html.join('');

        // 还原行内代码占位符
        text = text.replace(/\u0000CODE(\d+)\u0000/g, function (m, idx) {
            return codeSpans[parseInt(idx, 10)];
        });

        // 移除末尾多余 <br>
        text = text.replace(/(<br>)+$/, '');
        return text;
    }

    /**
     * 创建带属性的元素，简化批量构建 DOM 的代码。
     */
    function createElement(tag, className, textContent) {
        var el = document.createElement(tag);
        if (className) el.className = className;
        if (textContent !== undefined) el.textContent = textContent;
        return el;
    }

    /**
     * 将对话区滚动到底部，保证最新消息可见。
     */
    function scrollToBottom() {
        messageList.scrollTop = messageList.scrollHeight;
    }

    /**
     * 自适应输入框高度：内容增多时增高，上限 120px。
     */
    function autoGrowInput() {
        messageInput.style.height = "auto";
        messageInput.style.height = Math.min(messageInput.scrollHeight, 120) + "px";
    }

    /**
     * 读取并持久化 API Key 到 localStorage，便于生产环境复用。
     */
    function loadApiKey() {
        var saved = localStorage.getItem(API_KEY_STORAGE);
        if (saved) apiKeyInput.value = saved;
    }

    function saveApiKey() {
        var value = apiKeyInput.value.trim();
        if (value) {
            localStorage.setItem(API_KEY_STORAGE, value);
        } else {
            localStorage.removeItem(API_KEY_STORAGE);
        }
    }

    /**
     * 从 localStorage 加载流式模式偏好，默认开启。
     * 仅当显式存为 "false" 时关闭，兼容首次使用无存储的情况。
     */
    function loadStreamMode() {
        var saved = localStorage.getItem(STREAM_MODE_STORAGE);
        streamMode = saved !== "false";
        if (streamToggle) streamToggle.checked = streamMode;
    }

    /**
     * 持久化流式模式偏好，便于用户下次访问时复用。
     */
    function saveStreamMode() {
        localStorage.setItem(STREAM_MODE_STORAGE, String(streamMode));
    }

    // ===== 消息渲染函数 =====

    /**
     * 渲染一条消息行（用户或机器人），返回消息行元素。
     */
    function createMessageRow(role) {
        var row = createElement("div", "message message--" + role);
        var bubble = createElement("div", "bubble");
        row.appendChild(bubble);
        messageList.appendChild(row);
        return { row: row, bubble: bubble };
    }

    /**
     * 渲染用户消息：右侧蓝色气泡。
     */
    function appendUserMessage(text) {
        var parts = createMessageRow("user");
        parts.bubble.textContent = text;
        scrollToBottom();
    }

    /**
     * 渲染机器人消息：左侧白色气泡 + 可折叠的来源/置信度区。
     */
    function appendBotMessage(reply, data) {
        var parts = createMessageRow("bot");
        parts.bubble.textContent = reply;

        // 存在来源或置信度时追加折叠区
        if (data && (data.sources || data.confidence !== undefined)) {
            var meta = buildMetaBlock(data);
            parts.bubble.appendChild(meta);
        }

        scrollToBottom();
    }

    /**
     * 渲染错误消息：红色气泡提示。
     */
    function appendErrorMessage(text) {
        var parts = createMessageRow("error");
        parts.bubble.textContent = text;
        scrollToBottom();
    }

    /**
     * 构造可折叠的来源与置信度区块。
     */
    function buildMetaBlock(data) {
        var meta = createElement("div", "meta");

        // 折叠开关
        var toggle = createElement("div", "meta__toggle");
        var arrow = createElement("span", "meta__arrow", "▶");
        var label = createElement("span", null, "来源与置信度");
        toggle.appendChild(arrow);
        toggle.appendChild(label);
        meta.appendChild(toggle);

        // 折叠内容
        var content = createElement("div", "meta__content");

        // 置信度
        if (data.confidence !== undefined && data.confidence !== null) {
            var confidenceText = "置信度：" + (data.confidence * 100).toFixed(1) + "%";
            if (data.hit === false) confidenceText += "（未命中知识库）";
            content.appendChild(createElement("div", "meta__confidence", confidenceText));
        }

        // 来源列表
        if (data.sources && data.sources.length > 0) {
            data.sources.forEach(function (source) {
                content.appendChild(createElement("div", "meta__source-item", "• " + source));
            });
        } else if (data.hit !== false) {
            content.appendChild(createElement("div", "meta__source-item", "（无来源信息）"));
        }

        // 检索片段（可选，便于调试与查看原文）
        if (data.retrieved_chunks && data.retrieved_chunks.length > 0) {
            data.retrieved_chunks.forEach(function (chunk, index) {
                var chunkBlock = createElement("div", "meta__chunk");
                var header = "[片段" + (index + 1) + "] " +
                    (chunk.source || "未知来源") + " 第" + (chunk.page_number || 1) + "页" +
                    "（相似度 " + (chunk.score || 0).toFixed(3) + "）";
                chunkBlock.textContent = header + "\n" + (chunk.text || "");
                content.appendChild(chunkBlock);
            });
        }

        meta.appendChild(content);

        // 点击切换折叠状态
        toggle.addEventListener("click", function () {
            var isOpen = content.classList.toggle("is-open");
            if (isOpen) {
                arrow.classList.add("is-open");
            } else {
                arrow.classList.remove("is-open");
            }
        });

        return meta;
    }

    /**
     * 显示"客服正在输入..."动画，返回该消息行以便后续移除。
     */
    function showTypingIndicator() {
        var parts = createMessageRow("bot");
        var typing = createElement("div", "typing");
        typing.appendChild(createElement("span", "typing__dot"));
        typing.appendChild(createElement("span", "typing__dot"));
        typing.appendChild(createElement("span", "typing__dot"));
        parts.bubble.appendChild(typing);
        scrollToBottom();
        return parts.row;
    }

    // ===== SSE 流式解析与渲染辅助函数 =====

    /**
     * 解析 SSE 缓冲区，提取完整事件并返回剩余不完整部分。
     * SSE 事件以空行（\n\n）分隔，单个事件的 data 字段可能跨多行需拼接。
     * @param {string} buffer - 累积的原始文本
     * @returns {{events: Array<{type:string, data:string}>, buffer: string}}
     */
    function parseSSEChunk(buffer) {
        var events = [];
        // 按空行分割，最后一段可能不完整（缺少结尾 \n\n），保留为新的 buffer
        var parts = buffer.split(SSE_EVENT_SEPARATOR);
        buffer = parts.pop() || "";

        for (var i = 0; i < parts.length; i++) {
            var block = parts[i];
            if (!block.trim()) continue;

            var eventType = "";
            var dataLines = [];
            var lines = block.split("\n");
            for (var j = 0; j < lines.length; j++) {
                var line = lines[j];
                if (line.indexOf("event: ") === 0) {
                    eventType = line.slice(7);
                } else if (line.indexOf("data: ") === 0) {
                    dataLines.push(line.slice(6));
                } else if (line.indexOf("data:") === 0) {
                    // 兼容无空格的 data: 前缀
                    dataLines.push(line.slice(5));
                }
            }

            // 仅当事件类型与数据均存在时才视为有效事件
            if (eventType && dataLines.length > 0) {
                events.push({ type: eventType, data: dataLines.join("\n") });
            }
        }

        return { events: events, buffer: buffer };
    }

    /**
     * 显示流式 meta 事件：意图与来源徽章。
     * meta 事件通常在 token 之前到达，用于展示检索元信息。
     * @param {HTMLElement} bubble - 当前消息气泡元素
     * @param {Object} data - meta 事件数据 {intent, sources, session_id}
     * @param {Object} ctx - 流式渲染上下文
     */
    function showStreamMeta(bubble, data, ctx) {
        // 保存后端返回的 session_id，保证多轮对话会话保持
        if (data.session_id) sessionId = data.session_id;

        // 防止重复渲染（后端可能重发 meta）
        if (ctx.metaRendered) return;
        ctx.metaRendered = true;

        var meta = createElement("div", "stream-meta");

        // 意图徽章
        if (data.intent) {
            var intentBadge = createElement(
                "span",
                "stream-meta__badge stream-meta__badge--intent"
            );
            intentBadge.textContent = "意图：" + data.intent;
            meta.appendChild(intentBadge);
        }

        // 来源徽章：仅显示数量，详细来源在 done 后可由折叠区展示
        if (data.sources && data.sources.length > 0) {
            var sourceBadge = createElement(
                "span",
                "stream-meta__badge stream-meta__badge--source"
            );
            sourceBadge.textContent = "来源：" + data.sources.length + " 条";
            meta.appendChild(sourceBadge);
        }

        bubble.appendChild(meta);
    }

    /**
     * 追加 token 到当前气泡，并触发打字光标效果。
     * 首次调用时创建文本节点容器并添加 is-typing 类。
     * @param {HTMLElement} bubble - 当前消息气泡元素
     * @param {string} token - 本次接收的 token 内容
     * @param {Object} ctx - 流式渲染上下文
     */
    function renderStreamToken(bubble, token, ctx) {
        ctx.started = true;

        // 首次收到 token 时创建文本节点容器
        if (!ctx.textNode) {
            var textSpan = createElement("span", "bubble__text");
            ctx.textNode = document.createTextNode("");
            textSpan.appendChild(ctx.textNode);
            bubble.appendChild(textSpan);
            // 添加打字光标类（CSS ::after 实现闪烁）
            bubble.classList.add("is-typing");
        }

        // 追加 token 到文本节点（textContent 自动转义，防 XSS）
        ctx.textNode.data += token;
        scrollToBottom();
    }

    /**
     * 完成流式消息：移除打字光标，处理转人工等终态信息。
     * @param {HTMLElement} bubble - 当前消息气泡元素
     * @param {Object} data - done 事件数据 {turn_count, escalate, session_id}
     * @param {Object} ctx - 流式渲染上下文
     */
    function finishStreamMessage(bubble, data, ctx) {
        // 移除打字光标
        bubble.classList.remove("is-typing");
        // 标记完成，避免后续事件误处理
        ctx.finished = true;

        // done 事件也可能携带 session_id（防御性保存）
        if (data && data.session_id) sessionId = data.session_id;

        // done 后全量 markdown 渲染：取出流式阶段累积的纯文本，替换为格式化 HTML
        // 避免流式阶段反复解析 markdown 导致闪烁
        var rawText = ctx.textNode ? ctx.textNode.data : "";
        if (rawText) {
            var textSpan = bubble.querySelector(".bubble__text");
            if (textSpan) textSpan.innerHTML = renderMarkdown(rawText);
        }

        // 转人工提示
        if (data && data.escalate) {
            var escalateTip = createElement("div", "stream-meta__escalate");
            escalateTip.textContent = "已为您转接人工客服";
            bubble.appendChild(escalateTip);
        }
    }

    /**
     * 分发处理单个 SSE 事件，根据事件类型调用对应渲染函数。
     * @param {{type:string, data:string}} event - 解析后的事件
     * @param {Object} ctx - 流式渲染上下文
     * @param {HTMLElement} typingRow - 加载动画行（首个事件到达时移除）
     */
    function handleStreamEvent(event, ctx, typingRow) {
        // 首个有效事件到达时移除加载动画（避免与流式内容并存）
        if (!ctx.started && (event.type === "meta" || event.type === "token")) {
            if (typingRow && typingRow.parentNode) {
                messageList.removeChild(typingRow);
            }
        }

        // JSON 解析异常隔离：单条事件解析失败不影响后续事件
        var data = {};
        try {
            data = JSON.parse(event.data);
        } catch (err) {
            console.error("SSE data JSON 解析失败：", err, event.data);
            return;
        }

        switch (event.type) {
            case "meta":
                ctx.started = true;
                showStreamMeta(ctx.bubble, data, ctx);
                break;
            case "token":
                if (data.content) {
                    renderStreamToken(ctx.bubble, data.content, ctx);
                }
                break;
            case "done":
                finishStreamMessage(ctx.bubble, data, ctx);
                break;
            case "error":
                // error 事件：在当前气泡内追加错误提示（保留已渲染内容）
                ctx.started = true;
                ctx.bubble.classList.remove("is-typing");
                var errTip = createElement("div", "stream-error");
                errTip.textContent = "⚠ " + (data.message || "未知错误");
                ctx.bubble.appendChild(errTip);
                ctx.finished = true;
                break;
            default:
                console.warn("未知的 SSE 事件类型：", event.type);
        }
    }

    // ===== 业务函数 =====

    /**
     * 调用后端对话接口，返回 Promise<响应 JSON>。
     * 失败时抛出带可读消息的 Error。
     */
    function callChatApi(message, channel, apiKey) {
        var headers = { "Content-Type": "application/json" };
        if (apiKey) headers["X-API-Key"] = apiKey;

        var body = { message: message, channel: channel };
        if (sessionId) body.session_id = sessionId;

        return fetch(CHAT_API_URL, {
            method: "POST",
            headers: headers,
            body: JSON.stringify(body),
        }).then(function (response) {
            // 非 2xx 状态码：尝试解析后端错误信息
            if (!response.ok) {
                return response.json().then(
                    function (errBody) {
                        throw new Error(errBody.detail || "请求失败（HTTP " + response.status + "）");
                    },
                    function () {
                        throw new Error("请求失败（HTTP " + response.status + "）");
                    }
                );
            }
            return response.json();
        });
    }

    /**
     * 调用流式对话端点并消费 SSE 流。
     * 流式初始化失败（fetch 异常或响应非 200）时 reject，便于上层降级到非流式。
     * 一旦开始接收内容（ctx.started=true），中途异常不再抛出，保留已渲染内容。
     * @param {string} message - 用户消息
     * @param {string} channel - 渠道
     * @param {string} apiKey - API Key
     * @param {HTMLElement} typingRow - 加载动画行
     * @returns {Promise<void>}
     */
    function sendStreamMessage(message, channel, apiKey, typingRow, existingCtx) {
        var headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream"
        };
        if (apiKey) headers["X-API-Key"] = apiKey;

        var body = { message: message, channel: channel };
        if (sessionId) body.session_id = sessionId;

        return fetch(CHAT_STREAM_API_URL, {
            method: "POST",
            headers: headers,
            body: JSON.stringify(body),
        }).then(function (response) {
            // 非 200：抛出错误触发降级（SSE 协议约定错误也返回 200，此处防御性处理）
            if (!response.ok) {
                throw new Error("流式请求失败（HTTP " + response.status + "）");
            }
            // 防御性检查：响应体不可读时降级
            if (!response.body) {
                throw new Error("流式响应无可读体");
            }
            // 构造重连信息：流式中途断线时由 consumeSSEStream 触发一次重连
            var retryInfo = { message: message, channel: channel, apiKey: apiKey };
            // existingCtx 用于重连时复用气泡，首次调用时不传（undefined）
            return consumeSSEStream(response.body, typingRow, retryInfo, existingCtx);
        });
    }

    /**
     * 消费 SSE 流：逐块读取、解析、分发渲染。
     * 流式初始化阶段（未渲染任何内容）异常时抛出，触发降级；
     * 已开始渲染后异常则保留已有内容并追加错误提示，不再降级；
     * 流式中途断线（已开始但未完成）且未重连过时，触发一次自动重连。
     * @param {ReadableStream} body - fetch 响应体
     * @param {HTMLElement} typingRow - 加载动画行
     * @param {Object} [retryInfo] - 重连所需参数 {message, channel, apiKey}
     * @param {Object} [existingCtx] - 重连时复用的流式上下文（首次调用不传）
     * @returns {Promise<void>}
     */
    function consumeSSEStream(body, typingRow, retryInfo, existingCtx) {
        var reader = body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";

        // 流式渲染上下文：跨事件共享状态
        // 重连场景复用 existingCtx，避免创建新气泡
        var ctx;
        if (existingCtx) {
            ctx = existingCtx;
            // 重连：清空当前气泡已渲染内容，从头接收（避免复杂去重逻辑）
            if (ctx.bubble) ctx.bubble.innerHTML = "";
            ctx.started = false;
            ctx.finished = false;
            ctx.metaRendered = false;
            ctx.textNode = null;
        } else {
            ctx = {
                started: false,        // 是否已开始渲染（收到首个 token/meta/error）
                finished: false,       // 是否已完成（done/error）
                metaRendered: false,   // meta 是否已渲染（防重复）
                retried: false,        // 是否已触发过断线重连（限制仅重连一次，防死循环）
                bubble: null,          // 当前消息气泡元素
                row: null,             // 当前消息行元素（用于异常时移除空气泡）
                textNode: null         // token 文本节点引用，便于高效追加
            };
            // 创建消息气泡（占位，等待 meta/token 填充）
            var parts = createMessageRow("bot");
            ctx.bubble = parts.bubble;
            ctx.row = parts.row;
        }

        /**
         * 递归读取下一块并处理，直到流结束。
         */
        function pump() {
            return reader.read().then(function (result) {
                if (result.done) {
                    // 流正常结束：清理光标
                    if (ctx.bubble) {
                        ctx.bubble.classList.remove("is-typing");
                    }
                    // 若无任何内容被渲染，提示空响应
                    if (!ctx.started && ctx.bubble) {
                        if (typingRow && typingRow.parentNode) {
                            messageList.removeChild(typingRow);
                        }
                        ctx.bubble.textContent = "（空响应）";
                    }
                    return;
                }

                try {
                    buffer += decoder.decode(result.value, { stream: true });
                    var parsed = parseSSEChunk(buffer);
                    buffer = parsed.buffer;

                    // 逐个处理已完整的 SSE 事件
                    for (var i = 0; i < parsed.events.length; i++) {
                        handleStreamEvent(parsed.events[i], ctx, typingRow);
                        if (ctx.finished) break;
                    }
                } catch (err) {
                    // SSE 解析异常不中断流，记录到控制台继续读取
                    console.error("SSE 解析异常：", err);
                }

                // 继续读取下一块
                return pump();
            });
        }

        return pump().catch(function (err) {
            // reader.read 异常：清理状态后判断是否可降级或重连
            if (typingRow && typingRow.parentNode) {
                messageList.removeChild(typingRow);
            }
            if (ctx.bubble) {
                ctx.bubble.classList.remove("is-typing");
            }

            // 流式中途断线（已开始未完成）且未重连过：触发一次重连，从头接收
            // 重连策略简化：清空当前气泡已渲染内容重新渲染，避免复杂去重
            if (ctx.started && !ctx.finished && !ctx.retried && retryInfo) {
                ctx.retried = true;  // 标记已重连，防止死循环
                // 重连复用当前气泡，typingRow 已移除传 null
                return sendStreamMessage(
                    retryInfo.message, retryInfo.channel, retryInfo.apiKey, null, ctx
                ).catch(function () {
                    // 重连失败：保留已渲染内容，追加中断提示，不移除气泡
                    if (ctx.bubble) {
                        ctx.bubble.classList.remove("is-typing");
                        var tip = createElement("div", "stream-error");
                        tip.textContent = "连接中断，已显示部分回复";
                        ctx.bubble.appendChild(tip);
                    }
                });
            }

            // 已开始渲染内容：保留已有内容，追加错误提示，不再降级（避免重复显示）
            if (ctx.started) {
                var errTip = createElement("div", "stream-error");
                errTip.textContent = "⚠ 流式中断：" + (err && err.message ? err.message : "未知错误");
                ctx.bubble.appendChild(errTip);
                return;
            }

            // 重连后仍未开始渲染就异常：不移除气泡，追加中断提示
            if (ctx.retried) {
                if (ctx.bubble) {
                    var retryTip = createElement("div", "stream-error");
                    retryTip.textContent = "连接中断，已显示部分回复";
                    ctx.bubble.appendChild(retryTip);
                }
                return;
            }

            // 首次调用且未开始渲染：移除空气泡，抛出错误触发上层降级到非流式
            if (ctx.row && ctx.row.parentNode) {
                messageList.removeChild(ctx.row);
            }
            throw err;
        });
    }

    /**
     * 处理发送：校验输入 → 渲染用户消息 → 显示加载 → 调用接口 → 渲染回复。
     * 流式模式开启时走 SSE 流式；流式初始化失败自动降级到非流式重试。
     */
    function handleSend() {
        if (isSending) return;

        var message = messageInput.value.trim();
        if (!message) return;

        var channel = channelSelect.value;
        var apiKey = apiKeyInput.value.trim();

        // 锁定发送状态，清空输入框
        isSending = true;
        sendButton.disabled = true;
        messageInput.value = "";
        autoGrowInput();

        // 渲染用户消息
        appendUserMessage(message);

        // 显示加载动画
        var typingRow = showTypingIndicator();

        // 根据流式模式选择请求路径
        var promise;
        if (streamMode) {
            promise = sendStreamMessage(message, channel, apiKey, typingRow).catch(function (err) {
                // 流式初始化失败：降级到非流式重试（已渲染内容的情况由 consumeSSEStream 内部处理，不会走到这里）
                console.warn("流式失败，降级非流式：", err && err.message);
                // 防御性清理：确保加载动画已移除
                if (typingRow && typingRow.parentNode) {
                    messageList.removeChild(typingRow);
                }
                return callChatApi(message, channel, apiKey).then(function (data) {
                    if (data.session_id) sessionId = data.session_id;
                    appendBotMessage(data.reply, data.data);
                });
            });
        } else {
            promise = callChatApi(message, channel, apiKey)
                .then(function (data) {
                    messageList.removeChild(typingRow);
                    if (data.session_id) sessionId = data.session_id;
                    appendBotMessage(data.reply, data.data);
                })
                .catch(function (err) {
                    messageList.removeChild(typingRow);
                    appendErrorMessage("出错啦：" + err.message);
                });
        }

        // 统一恢复发送状态（成功或失败均执行）
        promise.then(function () {
            isSending = false;
            sendButton.disabled = false;
            messageInput.focus();
        }, function (err) {
            // 兜底错误处理（降级路径已处理大部分错误，此处防御性兜底）
            if (typingRow && typingRow.parentNode) {
                messageList.removeChild(typingRow);
            }
            appendErrorMessage("出错啦：" + (err && err.message ? err.message : "未知错误"));
            isSending = false;
            sendButton.disabled = false;
            messageInput.focus();
        });
    }

    // ===== 事件绑定 =====

    /**
     * 输入框按键处理：回车发送，Shift+回车换行。
     */
    messageInput.addEventListener("keydown", function (event) {
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            handleSend();
        }
    });

    // 输入时自适应高度
    messageInput.addEventListener("input", autoGrowInput);

    // 发送按钮
    sendButton.addEventListener("click", handleSend);

    // API Key 变更时持久化
    apiKeyInput.addEventListener("change", saveApiKey);
    apiKeyInput.addEventListener("blur", saveApiKey);

    // 流式模式开关：切换时更新状态并持久化
    if (streamToggle) {
        streamToggle.addEventListener("change", function () {
            streamMode = streamToggle.checked;
            saveStreamMode();
        });
    }

    // ===== 初始化 =====
    loadApiKey();
    loadStreamMode();
    appendBotMessage(WELCOME_TEXT, null);
    messageInput.focus();
})();
