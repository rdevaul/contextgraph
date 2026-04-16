/**
 * ContextEngine implementation for contextgraph plugin
 *
 * This is a SWITCHABLE engine that can operate in two modes:
 * - OFF: pass-through (returns messages as-is, like LegacyContextEngine)
 * - ON: calls Python API for graph-based assembly
 */
import { homedir } from "os";
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "fs";
import { join } from "path";
import { ContextGraphAPIClient } from "./api-client.js";
export class ContextGraphEngine {
    info = {
        id: "contextgraph",
        name: "Context Graph",
        version: "1.0.0",
        ownsCompaction: true, // graph doesn't need lossy compaction
    };
    client;
    stateDir;
    stateFile;
    comparisonLogFile;
    constructor() {
        this.client = new ContextGraphAPIClient();
        this.stateDir = join(homedir(), ".tag-context");
        this.stateFile = join(this.stateDir, "graph-mode.json");
        this.comparisonLogFile = join(this.stateDir, "comparison-log.jsonl");
        // Ensure state directory exists
        if (!existsSync(this.stateDir)) {
            mkdirSync(this.stateDir, { recursive: true });
        }
        // Initialize state file if it doesn't exist
        if (!existsSync(this.stateFile)) {
            this.saveState({ enabled: false });
        }
    }
    loadState() {
        try {
            const data = readFileSync(this.stateFile, "utf-8");
            return JSON.parse(data);
        }
        catch {
            return { enabled: false };
        }
    }
    saveState(state) {
        writeFileSync(this.stateFile, JSON.stringify(state, null, 2));
    }
    isGraphModeEnabled() {
        return this.loadState().enabled;
    }
    /**
     * Infer a channel label from a session ID.
     * Used to scope messages to a specific user for user-tag retrieval.
     *
     * Extracts the user segment from session patterns like:
     *   agent:<prefix>-<user>:<channel>  →  <user>
     * Examples:
     *   agent:jarvis-rich:main       →  "rich"
     *   agent:glados-dana:main       →  "dana"
     *   agent:glados-household:cron  →  "household"
     *
     * Falls back to the full sessionId if the pattern doesn't match.
     */
    inferChannelLabel(sessionId) {
        if (sessionId.includes("cron:"))
            return "cron";
        // Pattern: agent:<prefix>-<user>:<rest>
        // e.g. agent:glados-rich:main → "rich"
        const match = sessionId.match(/^agent:[^-]+-([^:]+):/);
        if (match)
            return match[1];
        // Fallback: UUID session IDs won't match the pattern.
        // Return the raw sessionId so callers can decide; user-tag
        // scoping will silently load no user tags (safe degradation).
        return sessionId;
    }
    detectToolChains(messages) {
        const pendingChainIds = [];
        let lastTurnHadTools = false;
        // Walk backwards through messages to find tool_use/tool_result pairs
        for (let i = messages.length - 1; i >= 0; i--) {
            const msg = messages[i];
            const role = msg.role;
            // Check if assistant messages contain tool_use content blocks
            if (role === "assistant") {
                const content = msg.content;
                let hasToolUse = false;
                if (Array.isArray(content)) {
                    hasToolUse = content.some((block) => block.type === "tool_use");
                }
                if (hasToolUse) {
                    // This assistant turn used tools
                    if (i >= messages.length - 3) {
                        // It's one of the most recent turns
                        lastTurnHadTools = true;
                    }
                    // Collect message IDs from the tool chain (this msg + surrounding tool_results)
                    if (msg.id)
                        pendingChainIds.push(msg.id);
                    // Also collect adjacent tool_result messages
                    for (let j = i + 1; j < messages.length && j <= i + 5; j++) {
                        const next = messages[j];
                        if (next.role === "tool" || next.role === "tool_result") {
                            if (next.id)
                                pendingChainIds.push(next.id);
                        }
                        else if (next.role === "user") {
                            // Also pin the user message that triggered the tool chain
                            break;
                        }
                    }
                    // Pin the preceding user message that initiated the chain
                    if (i > 0 && messages[i - 1].role === "user") {
                        if (messages[i - 1].id)
                            pendingChainIds.push(messages[i - 1].id);
                    }
                }
            }
            // Only look back through the last ~20 messages for tool chains
            if (messages.length - i > 20)
                break;
        }
        return {
            last_turn_had_tools: lastTurnHadTools,
            pending_chain_ids: [...new Set(pendingChainIds)], // deduplicate
        };
    }
    extractMessageText(message) {
        const raw = message.content || "";
        const text = typeof raw === "string"
            ? raw
            : Array.isArray(raw)
                ? raw.filter((b) => b.type === "text").map((b) => b.text).join("\n")
                : String(raw);
        if (message.role === "user") {
            return { user_text: text, assistant_text: "" };
        }
        else if (message.role === "assistant") {
            return { user_text: "", assistant_text: text };
        }
        return { user_text: text, assistant_text: "" };
    }
    estimateTokens(messages) {
        let total = 0;
        for (const msg of messages) {
            const text = msg.content || "";
            // Rough estimate: 1 token ≈ 0.75 words
            total += Math.ceil((text.split(/\s+/).length * 4) / 3);
        }
        return total;
    }
    async ingest(params) {
        if (!this.isGraphModeEnabled()) {
            // Pass-through mode: don't ingest
            return { ingested: false };
        }
        try {
            const { user_text, assistant_text } = this.extractMessageText(params.message);
            await this.client.ingest({
                id: params.message.id,
                session_id: params.sessionId,
                user_text,
                assistant_text,
                timestamp: Date.now() / 1000,
                user_id: "openclaw",
                external_id: params.message.id, // Pass OpenClaw message ID as external_id
                channel_label: this.inferChannelLabel(params.sessionId),
            });
            return { ingested: true };
        }
        catch (error) {
            console.error("contextgraph ingest error:", error);
            return { ingested: false };
        }
    }
    async assemble(params) {
        if (!this.isGraphModeEnabled()) {
            // Pass-through mode: return messages as-is
            return {
                messages: params.messages,
                estimatedTokens: this.estimateTokens(params.messages),
            };
        }
        try {
            // Get last user message for tag inference
            const userMessages = params.messages.filter((m) => m.role === "user");
            const lastUserMessage = userMessages[userMessages.length - 1];
            // content can be a string or an array of content blocks
            const rawContent = lastUserMessage?.content || "";
            const userText = typeof rawContent === "string"
                ? rawContent
                : Array.isArray(rawContent)
                    ? rawContent.filter((b) => b.type === "text").map((b) => b.text).join("\n")
                    : String(rawContent);
            // Use a sensible budget for graph assembly, not the full context window
            const budget = Math.min(params.tokenBudget || 4000, 8000);
            // Detect tool chains in the message history for sticky pinning
            const toolState = this.detectToolChains(params.messages);
            console.log("[contextgraph] assemble:", {
                userTextLength: userText?.length,
                hasToolChain: toolState.last_turn_had_tools,
                pendingChains: toolState.pending_chain_ids.length,
            });
            const result = await this.client.assemble(userText, undefined, budget, toolState);
            // Convert Python API messages to AgentMessage format
            const assembledMessages = [];
            for (const msg of result.messages) {
                // Create user message — content must be array of content blocks
                if (msg.user_text) {
                    assembledMessages.push({
                        id: `${msg.id}-user`,
                        role: "user",
                        content: [{ type: "text", text: msg.user_text }],
                    });
                }
                // Create assistant message
                if (msg.assistant_text) {
                    assembledMessages.push({
                        id: `${msg.id}-assistant`,
                        role: "assistant",
                        content: [{ type: "text", text: msg.assistant_text }],
                    });
                }
            }
            return {
                messages: assembledMessages,
                estimatedTokens: result.total_tokens,
                systemPromptAddition: `Context assembled via graph (recency: ${result.recency_count}, topic: ${result.topic_count}, tags: ${result.tags_used.join(", ")})`,
            };
        }
        catch (error) {
            console.error("contextgraph assemble error:", error);
            // Fallback to pass-through on error
            return {
                messages: params.messages,
                estimatedTokens: this.estimateTokens(params.messages),
            };
        }
    }
    async compact() {
        // Graph doesn't need compaction — it grows and retrieves
        return {
            ok: true,
            compacted: false,
            reason: "graph-engine-no-compaction",
        };
    }
    async afterTurn(params) {
        if (!this.isGraphModeEnabled()) {
            return; // Nothing to do in pass-through mode
        }
        try {
            // Find the last user and assistant messages in this turn
            const newMessages = params.messages.slice(params.prePromptMessageCount);
            const userMsg = newMessages.find((m) => m.role === "user");
            const assistantMsg = newMessages.find((m) => m.role === "assistant");
            if (!userMsg || !assistantMsg) {
                return; // No complete turn to log
            }
            const rawUserContent = userMsg.content || "";
            const userText = typeof rawUserContent === "string"
                ? rawUserContent
                : Array.isArray(rawUserContent)
                    ? rawUserContent.filter((b) => b.type === "text").map((b) => b.text).join("\n")
                    : String(rawUserContent);
            const rawAssistantContent = assistantMsg.content || "";
            const assistantText = typeof rawAssistantContent === "string"
                ? rawAssistantContent
                : Array.isArray(rawAssistantContent)
                    ? rawAssistantContent.filter((b) => b.type === "text").map((b) => b.text).join("\n")
                    : String(rawAssistantContent);
            // Get comparison data from API
            console.log("[contextgraph] afterTurn calling compare:", {
                userTextLength: userText?.length,
                assistantTextLength: assistantText?.length,
                userTextPreview: userText?.slice(0, 80),
            });
            const comparison = await this.client.compare(userText, assistantText);
            // Log to comparison file
            const logEntry = {
                timestamp: new Date().toISOString(),
                session_id: params.sessionId,
                query_preview: userText.slice(0, 60) + (userText.length > 60 ? "..." : ""),
                graph_assembly: {
                    messages: comparison.graph_assembly.messages.length,
                    tokens: comparison.graph_assembly.total_tokens,
                    recency: comparison.graph_assembly.recency_count,
                    topic: comparison.graph_assembly.topic_count,
                    tags: comparison.graph_assembly.tags_used,
                },
                linear_would_have: {
                    messages: comparison.linear_window.messages.length,
                    tokens: comparison.linear_window.total_tokens,
                    tags_present: comparison.linear_window.tags_used,
                },
            };
            // Append to JSONL log
            const logLine = JSON.stringify(logEntry) + "\n";
            writeFileSync(this.comparisonLogFile, logLine, { flag: "a" });
            // Ingest the assistant response for future retrievals
            await this.client.ingest({
                id: assistantMsg.id,
                session_id: params.sessionId,
                user_text: userText,
                assistant_text: assistantText,
                timestamp: Date.now() / 1000,
                user_id: "openclaw",
                external_id: assistantMsg.id, // Pass OpenClaw message ID as external_id
                channel_label: this.inferChannelLabel(params.sessionId),
            });
        }
        catch (error) {
            console.error("contextgraph afterTurn error:", error);
        }
    }
    async dispose() {
        // Nothing to clean up
    }
}
