/**
 * contextgraph — OpenClaw ContextEngine Plugin
 *
 * Bridges OpenClaw's ContextEngine interface to the contextgraph Python
 * FastAPI server running at http://localhost:8300.
 *
 * When graph mode is OFF (default): transparent pass-through — behaves
 * identically to the legacy linear context window.
 *
 * When graph mode is ON (/graph on): routes bootstrap/ingest/assemble
 * through the Python API for semantic, topic-tagged context assembly.
 *
 * Toggle via: /graph on | /graph off | /graph (status)
 */

import type { OpenClawPluginApi } from "@openclaw/plugin-sdk";
import type {
  ContextEngine,
  ContextEngineInfo,
  AssembleResult,
  CompactResult,
  IngestResult,
  IngestBatchResult,
  BootstrapResult,
} from "@openclaw/plugin-sdk/context-engine/types";
import type { AgentMessage } from "@mariozechner/pi-agent-core";
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";

// ── Constants ──────────────────────────────────────────────────────────────

const PYTHON_API_BASE = "http://localhost:8300";
const REQUEST_TIMEOUT_MS = 5000;
const GRAPH_MODE_FILE = path.join(os.homedir(), ".tag-context", "graph-mode.json");
const GRAPH_MODE_DIR = path.dirname(GRAPH_MODE_FILE);

// ── State helpers ──────────────────────────────────────────────────────────

function ensureGraphModeDir(): void {
  if (!fs.existsSync(GRAPH_MODE_DIR)) {
    fs.mkdirSync(GRAPH_MODE_DIR, { recursive: true });
  }
}

function readGraphMode(): boolean {
  try {
    if (!fs.existsSync(GRAPH_MODE_FILE)) {
      return false; // safe default
    }
    const raw = fs.readFileSync(GRAPH_MODE_FILE, "utf8");
    const parsed = JSON.parse(raw);
    return Boolean(parsed?.enabled);
  } catch {
    return false;
  }
}

function writeGraphMode(enabled: boolean): void {
  ensureGraphModeDir();
  fs.writeFileSync(GRAPH_MODE_FILE, JSON.stringify({ enabled }, null, 2), "utf8");
}

// ── HTTP helper ────────────────────────────────────────────────────────────

async function apiPost(
  endpoint: string,
  body: unknown,
  logger: OpenClawPluginApi["logger"]
): Promise<unknown | null> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    const response = await fetch(`${PYTHON_API_BASE}${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    clearTimeout(timer);
    if (!response.ok) {
      logger.warn(`contextgraph: ${endpoint} returned ${response.status}`);
      return null;
    }
    return await response.json();
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.includes("abort") || msg.includes("ECONNREFUSED") || msg.includes("fetch failed")) {
      logger.warn(`contextgraph: Python API unreachable at ${endpoint} — falling back to pass-through`);
    } else {
      logger.error(`contextgraph: ${endpoint} error — ${msg}`);
    }
    return null;
  }
}

async function apiGet(
  endpoint: string,
  logger: OpenClawPluginApi["logger"]
): Promise<unknown | null> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    const response = await fetch(`${PYTHON_API_BASE}${endpoint}`, {
      signal: controller.signal,
    });
    clearTimeout(timer);
    if (!response.ok) return null;
    return await response.json();
  } catch {
    return null;
  }
}

// ── Comparison logging ─────────────────────────────────────────────────────

const COMPARISON_LOG_PATH = path.join(os.homedir(), ".tag-context", "comparison-log.jsonl");

function writeComparisonLog(entry: Record<string, unknown>): void {
  try {
    ensureGraphModeDir();
    const line = JSON.stringify(entry) + "\n";
    fs.appendFileSync(COMPARISON_LOG_PATH, line, "utf8");
  } catch {
    // Non-fatal
  }
}

// ── Safe recent context slice ──────────────────────────────────────────────
//
// Slicing mid-sequence breaks tool_use/tool_result pairing, which the Claude
// API rejects. Walk backward from the desired start until we land on a message
// that is safe to be first: a user message with no tool_result blocks, or an
// assistant message with no tool_use blocks.

function hasToolResult(msg: AgentMessage): boolean {
  // OpenClaw internal format uses role="toolResult" for tool result messages.
  // Anthropic API format uses role="user" with content[].type="tool_result".
  if ((msg as { role?: string }).role === "toolResult") return true;
  const content = (msg as { content?: unknown }).content;
  if (!Array.isArray(content)) return false;
  return content.some(
    (b) => b && typeof b === "object" &&
      ((b as { type?: string }).type === "tool_result" ||
       (b as { type?: string }).type === "toolResult")
  );
}

function hasToolUse(msg: AgentMessage): boolean {
  // OpenClaw internal format uses type="toolCall"; Anthropic uses type="tool_use".
  const content = (msg as { content?: unknown }).content;
  if (!Array.isArray(content)) return false;
  return content.some(
    (b) => b && typeof b === "object" &&
      ((b as { type?: string }).type === "tool_use" ||
       (b as { type?: string }).type === "toolCall")
  );
}

function safeRecentSlice(messages: AgentMessage[], maxMessages: number): AgentMessage[] {
  if (messages.length === 0) return [];
  let startIdx = Math.max(0, messages.length - maxMessages);

  // Walk backward until the first message in the slice is safe to be first.
  // A user message with tool_result needs the preceding assistant tool_use.
  // An assistant message with tool_use needs a preceding user message.
  while (startIdx > 0) {
    const first = messages[startIdx];
    // Use hasToolResult/hasToolUse to cover both OpenClaw internal and Anthropic formats.
    if (hasToolResult(first)) {
      startIdx--;
    } else if (first.role === "assistant" && hasToolUse(first)) {
      startIdx--;
    } else {
      break;
    }
  }

  return messages.slice(startIdx);
}

// Remove orphaned tool_result/tool_use blocks from a message array.
// Applied as a safety net over the full assembled array — handles cases
// where safeRecentSlice couldn't back up far enough (e.g. tool pair at
// index 0) or where the graph/slice junction creates mismatched pairs.
// Also handles empty-content assistant messages (content: []) which would
// fail the hasToolUse check and cause false orphans.
function removeOrphanedToolPairs(
  messages: AgentMessage[],
  logger?: { warn: (msg: string) => void }
): AgentMessage[] {
  const result: AgentMessage[] = [];
  for (let i = 0; i < messages.length; i++) {
    const msg = messages[i];
    if (hasToolResult(msg)) {
      // Walk back through result to find the nearest assistant message.
      // It must have a tool_use/toolCall block to be a valid pair.
      let matchFound = false;
      for (let j = result.length - 1; j >= 0; j--) {
        const candidate = result[j];
        if (candidate.role === "assistant") {
          if (hasToolUse(candidate)) {
            matchFound = true;
          }
          break; // stop at first assistant message regardless
        }
      }
      if (!matchFound) {
        logger?.warn(`contextgraph: dropping orphaned tool_result at assembled index ${result.length}`);
        continue;
      }
    }
    result.push(msg);
  }
  // Drop any trailing assistant tool_use with no following tool_result.
  while (result.length > 0) {
    const last = result[result.length - 1];
    if (last.role === "assistant" && hasToolUse(last)) {
      logger?.warn(`contextgraph: dropping trailing tool_use at assembled index ${result.length - 1}`);
      result.pop();
    } else {
      break;
    }
  }
  return result;
}

// ── Message text extraction ────────────────────────────────────────────────

function extractTextFromMessage(msg: AgentMessage): string | null {
  // Only ingest user and assistant text messages — skip tool calls/results
  if (msg.role !== "user" && msg.role !== "assistant") {
    return null;
  }
  if (typeof (msg as { content?: unknown }).content === "string") {
    return (msg as { content: string }).content;
  }
  const content = (msg as { content?: unknown[] }).content;
  if (Array.isArray(content)) {
    const texts: string[] = [];
    for (const block of content) {
      if (
        block &&
        typeof block === "object" &&
        "type" in block &&
        (block as { type: string }).type === "text" &&
        "text" in block
      ) {
        texts.push((block as { text: string }).text);
      }
    }
    return texts.length > 0 ? texts.join("\n") : null;
  }
  return null;
}

function splitUserAssistant(messages: AgentMessage[]): {
  userParts: string[];
  assistantParts: string[];
} {
  const userParts: string[] = [];
  const assistantParts: string[] = [];
  for (const msg of messages) {
    const text = extractTextFromMessage(msg);
    if (text) {
      if (msg.role === "user") userParts.push(text);
      else if (msg.role === "assistant") assistantParts.push(text);
    }
  }
  return { userParts, assistantParts };
}

// ── Context Engine implementation ──────────────────────────────────────────

function createContextGraphEngine(logger: OpenClawPluginApi["logger"]): ContextEngine {
  const info: ContextEngineInfo = {
    id: "contextgraph",
    name: "Context Graph Engine",
    version: "1.0.0",
    ownsCompaction: false,
  };

  // In-memory tracker for tool-use chain IDs (for sticky threads)
  const toolChainIds = new Map<string, string[]>();

  return {
    info,

    async bootstrap({ sessionId, sessionFile }): Promise<BootstrapResult> {
      if (!readGraphMode()) {
        return { bootstrapped: false, reason: "graph-mode-off" };
      }

      // Read JSONL session file and batch-ingest historical messages
      let messages: AgentMessage[] = [];
      try {
        if (!fs.existsSync(sessionFile)) {
          return { bootstrapped: true, importedMessages: 0 };
        }
        const lines = fs.readFileSync(sessionFile, "utf8").split("\n").filter(Boolean);
        messages = lines.map((l) => JSON.parse(l) as AgentMessage).filter(Boolean);
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.warn(`contextgraph bootstrap: failed to read session file — ${msg}`);
        return { bootstrapped: false, reason: `read-error: ${msg}` };
      }

      let ingestedCount = 0;
      const now = Date.now() / 1000;

      // Process messages pairwise (user → assistant)
      for (let i = 0; i < messages.length; i++) {
        const msg = messages[i];
        const userText = msg.role === "user" ? extractTextFromMessage(msg) : null;
        if (!userText) continue;

        // Look ahead for the matching assistant reply
        const nextMsg = messages[i + 1];
        const assistantText =
          nextMsg?.role === "assistant" ? (extractTextFromMessage(nextMsg) ?? "") : "";

        if (userText) {
          const result = await apiPost(
            "/ingest",
            {
              id: `bootstrap-${sessionId}-${i}`,
              external_id: `bootstrap-${sessionId}-${i}`,
              session_id: sessionId,
              user_text: userText,
              assistant_text: assistantText,
              timestamp: now,
            },
            logger
          );
          if (result) ingestedCount++;
        }
      }

      logger.info(`contextgraph: bootstrapped ${ingestedCount} messages from ${sessionFile}`);
      return { bootstrapped: true, importedMessages: ingestedCount };
    },

    async ingest({ sessionId, message }): Promise<IngestResult> {
      if (!readGraphMode()) {
        return { ingested: false };
      }

      const text = extractTextFromMessage(message);
      if (!text || message.role !== "user") {
        // Only ingest user messages here; assistant messages are ingested in afterTurn
        return { ingested: false };
      }

      const msgId = `msg-${sessionId}-${Date.now()}`;
      const result = await apiPost(
        "/ingest",
        {
          id: msgId,
          external_id: msgId,
          session_id: sessionId,
          user_text: text,
          assistant_text: "",
          timestamp: Date.now() / 1000,
        },
        logger
      );

      return { ingested: result != null };
    },

    async ingestBatch({ sessionId, messages }): Promise<IngestBatchResult> {
      if (!readGraphMode()) {
        return { ingestedCount: 0 };
      }

      const { userParts, assistantParts } = splitUserAssistant(messages);
      if (userParts.length === 0) return { ingestedCount: 0 };

      const batchId = `batch-${sessionId}-${Date.now()}`;
      const result = await apiPost(
        "/ingest",
        {
          id: batchId,
          external_id: batchId,
          session_id: sessionId,
          user_text: userParts.join("\n"),
          assistant_text: assistantParts.join("\n"),
          timestamp: Date.now() / 1000,
        },
        logger
      );

      return { ingestedCount: result != null ? userParts.length : 0 };
    },

    async assemble({ sessionId, messages, tokenBudget }): Promise<AssembleResult> {
      if (!readGraphMode()) {
        // Pass-through: still sanitize tool pairs before returning.
        const safe = removeOrphanedToolPairs(messages, logger);
        return {
          messages: safe,
          estimatedTokens: safe.length * 150,
        };
      }

      // Extract the last user message to drive assembly
      let lastUserText = "";
      for (let i = messages.length - 1; i >= 0; i--) {
        const msg = messages[i];
        if (msg.role === "user") {
          const text = extractTextFromMessage(msg);
          if (text) {
            lastUserText = text;
            break;
          }
        }
      }

      if (!lastUserText) {
        logger.warn("contextgraph assemble: no user message found — passing through");
        const safe = removeOrphanedToolPairs(messages, logger);
        return { messages: safe, estimatedTokens: safe.length * 150 };
      }

      // Cap graph retrieval at 8K tokens. The Python API internally splits
      // this 25% recency / 75% topic (per contextgraph design). System was
      // validated at 4K default; 8K gives more depth for dense research messages.
      const GRAPH_TOKEN_BUDGET = 8000;
      const budget = Math.min(tokenBudget ?? GRAPH_TOKEN_BUDGET, GRAPH_TOKEN_BUDGET);

      // Detect tool use in recent messages for sticky threads
      const recentAssistant = messages.filter(m => m.role === "assistant").slice(-5);
      const lastTurnHadTools = recentAssistant.some(m => hasToolUse(m));
      const pendingChainIds = lastTurnHadTools
        ? (toolChainIds.get(sessionId) ?? [])
        : [];

      const result = await apiPost(
        "/assemble",
        {
          user_text: lastUserText,
          token_budget: budget,
          tool_state: lastTurnHadTools
            ? { last_turn_had_tools: true, pending_chain_ids: pendingChainIds }
            : null,
        },
        logger
      );

      if (!result || typeof result !== "object") {
        logger.warn("contextgraph assemble: API failed — falling back to pass-through");
        const safe = removeOrphanedToolPairs(messages, logger);
        return { messages: safe, estimatedTokens: safe.length * 150 };
      }

      const data = result as {
        messages?: Array<{
          id: string;
          user_text: string;
          assistant_text: string;
          tags: string[];
          timestamp: number;
        }>;
        total_tokens?: number;
      };

      // Convert Python API messages back to AgentMessage format
      // Use content block arrays to match OpenClaw's internal format
      const assembled: AgentMessage[] = [];
      for (const m of data.messages ?? []) {
        if (m.user_text) {
          assembled.push({
            role: "user",
            content: [{ type: "text", text: m.user_text }],
          } as AgentMessage);
        }
        if (m.assistant_text) {
          assembled.push({
            role: "assistant",
            content: [{ type: "text", text: m.assistant_text }],
          } as AgentMessage);
        }
      }

      // Append only the most recent messages for immediate context —
      // graph retrieval already provides relevant history, so we don't
      // need to append the entire conversation (which causes token bloat).
      // Use safeRecentSlice to avoid splitting tool_use/tool_result pairs.
      const RECENT_CONTEXT_KEEP = 10;
      assembled.push(...safeRecentSlice(messages, RECENT_CONTEXT_KEEP));

      // Final safety pass: remove any orphaned tool pairs that slipped through
      // (e.g. tool pair at index 0, or mismatches at the graph/slice junction).
      const safe = removeOrphanedToolPairs(assembled, logger);

      const totalTokens = data.total_tokens ?? safe.length * 150;
      const stickyCount = (data as any).sticky_count ?? 0;

      logger.info(
        `contextgraph assemble: ${safe.length} msgs, ~${totalTokens} tok (graph: ${data.messages?.length ?? 0} retrieved, sticky: ${stickyCount}, tools: ${lastTurnHadTools})`
      );

      return {
        messages: safe,
        estimatedTokens: totalTokens,
        systemPromptAddition:
          "Context below includes semantically relevant historical messages retrieved by the graph engine.",
      };
    },

    async compact({ sessionId }): Promise<CompactResult> {
      if (!readGraphMode()) {
        return { ok: true, compacted: false, reason: "graph-off-defer-to-legacy" };
      }
      // Graph engine uses semantic retrieval — no compaction needed
      return { ok: true, compacted: false, reason: "graph-engine-no-compaction" };
    },

    async afterTurn({ sessionId, messages }): Promise<void> {
      if (!readGraphMode()) return;

      // Ingest new assistant messages from this turn
      const assistantMessages = messages.filter((m) => m.role === "assistant");
      if (assistantMessages.length === 0) return;

      const userMessages = messages.filter((m) => m.role === "user");
      const userText = userMessages
        .map((m) => extractTextFromMessage(m))
        .filter(Boolean)
        .join("\n");
      const assistantText = assistantMessages
        .map((m) => extractTextFromMessage(m))
        .filter(Boolean)
        .join("\n");

      if (!assistantText) return;

      // Use stable turnId for external_id tracking
      const turnId = `turn-${sessionId}-${Date.now()}`;

      // Detect tool use in this turn
      const hadTools = assistantMessages.some(m => hasToolUse(m));
      if (hadTools) {
        const ids = toolChainIds.get(sessionId) ?? [];
        ids.push(turnId);
        toolChainIds.set(sessionId, ids.slice(-10));
      }

      await apiPost(
        "/ingest",
        {
          id: turnId,
          external_id: turnId,
          session_id: sessionId,
          user_text: userText || "",
          assistant_text: assistantText,
          timestamp: Date.now() / 1000,
        },
        logger
      );

      // Comparison logging — non-fatal
      if (userText) {
        try {
          const comparison = await apiPost("/compare", {
            user_text: userText,
            assistant_text: assistantText || "",
          }, logger) as any;

          if (comparison) {
            writeComparisonLog({
              timestamp: new Date().toISOString(),
              sessionId,
              userText: userText.slice(0, 200),
              graphMsgCount: comparison.graph_assembly?.messages?.length ?? 0,
              graphTokens: comparison.graph_assembly?.total_tokens ?? 0,
              graphTags: comparison.graph_assembly?.tags_used ?? [],
              graphRecency: comparison.graph_assembly?.recency_count ?? 0,
              graphTopic: comparison.graph_assembly?.topic_count ?? 0,
              linearMsgCount: comparison.linear_window?.messages?.length ?? 0,
              linearTokens: comparison.linear_window?.total_tokens ?? 0,
              linearTags: comparison.linear_window?.tags_used ?? [],
              stickyPins: comparison.graph_assembly?.sticky_count ?? 0,
              hadTools,
            });
          }
        } catch {
          // Non-fatal — don't break the turn on logging failure
        }
      }
    },

    async dispose(): Promise<void> {
      // No-op — Python server manages its own lifecycle
    },
  };
}

// ── Plugin entry point ─────────────────────────────────────────────────────

export default function register(api: OpenClawPluginApi): void {
  const { logger } = api;

  logger.info("contextgraph: registering context engine");

  // Register the context engine factory
  api.registerContextEngine("contextgraph", () => createContextGraphEngine(logger));

  // Register /graph command
  api.registerCommand({
    name: "graph",
    description: "Toggle or check the context graph engine mode",
    acceptsArgs: true,
    handler: async (ctx) => {
      const arg = (ctx.args ?? "").trim().toLowerCase();

      if (arg === "on") {
        writeGraphMode(true);
        return { text: "🔀 Context graph engine activated. Using DAG-based context assembly." };
      }

      if (arg === "off") {
        writeGraphMode(false);
        return { text: "🔀 Switched back to linear context window." };
      }

      // Status check
      const enabled = readGraphMode();

      // Optionally ping the API to check health
      let apiStatus = "unknown";
      try {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 2000);
        const res = await fetch(`${PYTHON_API_BASE}/health`, { signal: controller.signal });
        clearTimeout(timer);
        if (res.ok) {
          const data = await res.json() as any;
          apiStatus = `✅ running (${data.messages_in_store ?? 0} messages stored)`;
        } else {
          apiStatus = `⚠️ error (${res.status})`;
        }
      } catch {
        apiStatus = "❌ unreachable";
      }

      const modeLabel = enabled ? "🟢 ON" : "⚪ OFF";
      return {
        text: `**Context Graph Engine**\nMode: ${modeLabel}\nPython API: ${apiStatus}\n\nUse \`/graph on\` or \`/graph off\` to toggle.`
      };
    },
  });

  logger.info("contextgraph: plugin ready (default: graph mode OFF)");
}
