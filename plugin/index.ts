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

const PYTHON_API_BASE = process.env.CONTEXTGRAPH_API_URL ?? "http://localhost:8300";
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

function countToolCalls(msg: AgentMessage): number {
  // Count discrete tool_use / toolCall blocks in a single message.
  const content = (msg as { content?: unknown }).content;
  if (!Array.isArray(content)) return 0;
  return content.filter(
    (b) => b && typeof b === "object" &&
      ((b as { type?: string }).type === "tool_use" ||
       (b as { type?: string }).type === "toolCall")
  ).length;
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

// ── Channel label inference ────────────────────────────────────────────────

/**
 * Channel label map: senderId → canonical username.
 *
 * Loaded once at startup. Supports many-to-one mappings so a user who
 * interacts via multiple channels (Telegram, Discord, etc.) gets a single
 * unified user-tag profile.
 *
 * Configuration (in priority order):
 *
 *   1. Config file: <sybilclaw-config-dir>/contextgraph/channel_labels.yaml
 *      Format:
 *        "994902066": rich           # Telegram - Rich
 *        "510637988242522133": rich  # Discord  - Rich
 *        "900606288": dana           # Telegram - Dana
 *
 *   2. Env var: CONTEXTGRAPH_SENDER_LABELS (JSON object)
 *      e.g. CONTEXTGRAPH_SENDER_LABELS='{"994902066":"rich"}'
 *
 *   If both are set, the config file takes precedence and a warning is logged.
 *   If neither is set, senderId is used directly (works for single-channel installs).
 */

const CONFIG_DIR = process.env.SYBILCLAW_CONFIG_DIR
  ?? process.env.OPENCLAW_CONFIG_DIR
  ?? path.join(os.homedir(), ".sybilclaw");
const CHANNEL_LABELS_FILE = path.join(CONFIG_DIR, "contextgraph", "channel_labels.yaml");

let _channelLabels: Record<string, string> | null = null;
let _channelLabelsSource: "file" | "env" | "none" | null = null;

function loadChannelLabels(logger?: OpenClawPluginApi["logger"]): Record<string, string> {
  if (_channelLabels !== null) return _channelLabels;

  const hasFile = fs.existsSync(CHANNEL_LABELS_FILE);
  const hasEnv = !!process.env.CONTEXTGRAPH_SENDER_LABELS;

  if (hasFile && hasEnv) {
    const warn = `[contextgraph] WARNING: Both channel_labels.yaml and CONTEXTGRAPH_SENDER_LABELS env var are set. Config file takes precedence. Unset the env var to silence this warning.`;
    logger?.warn(warn) ?? console.warn(warn);
  }

  if (hasFile) {
    try {
      // Simple YAML parse: only handles flat key: value string pairs
      const raw = fs.readFileSync(CHANNEL_LABELS_FILE, "utf8");
      const result: Record<string, string> = {};
      for (const line of raw.split("\n")) {
        const trimmed = line.replace(/#.*$/, "").trim();
        if (!trimmed) continue;
        const m = trimmed.match(/^["']?([^"':]+)["']?\s*:\s*["']?([^"'#\s]+)["']?/);
        if (m) result[m[1].trim()] = m[2].trim();
      }
      _channelLabels = result;
      _channelLabelsSource = "file";
      return result;
    } catch (e) {
      const err = `[contextgraph] Failed to parse channel_labels.yaml: ${e}. Falling back to env var or senderId.`;
      logger?.warn(err) ?? console.warn(err);
    }
  }

  if (hasEnv) {
    try {
      _channelLabels = JSON.parse(process.env.CONTEXTGRAPH_SENDER_LABELS!);
      _channelLabelsSource = "env";
      return _channelLabels!;
    } catch (e) {
      const err = `[contextgraph] Failed to parse CONTEXTGRAPH_SENDER_LABELS env var: ${e}. Falling back to senderId.`;
      logger?.warn(err) ?? console.warn(err);
    }
  }

  _channelLabels = {};
  _channelLabelsSource = "none";
  return {};
}

/**
 * Infer a channel label from a session ID and/or sender ID.
 * Used to scope user tags to the correct person.
 *
 * Resolution order:
 *   1. Session ID pattern: agent:<prefix>-<user>:<channel>  →  <user>
 *      e.g. agent:glados-rich:main → "rich" (structured deployments)
 *   2. channel_labels.yaml or CONTEXTGRAPH_SENDER_LABELS lookup by senderId
 *      Enables many-to-one mapping (same user on Telegram + Discord → same label)
 *   3. Raw senderId — consistent within a single channel, no config needed
 *   4. "unknown" — safe degradation, no user tags loaded
 */
function inferChannelLabel(senderId?: string, sessionId?: string, logger?: OpenClawPluginApi["logger"]): string {
  // 1. Try structured session key pattern
  if (sessionId) {
    const match = sessionId.match(/^agent:[^-]+-([^:]+):/);
    if (match) return match[1];
  }

  // 2. Try sender ID lookup (many-to-one, cross-channel unification)
  if (senderId) {
    const labels = loadChannelLabels(logger);
    if (labels[senderId]) return labels[senderId];
  }

  // 3. Raw senderId — works for single-channel installs with no config
  if (senderId) return senderId;

  return "unknown";
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

      // Detect tool use in the most recent assistant turn only — not the last N turns.
      // Sticky threads are for multi-turn operations (code reviews, extended dev work)
      // where losing thread continuity would break the work. Single incidental tool calls
      // (heartbeats, memory lookups, simple queries) should NOT activate sticky.
      //
      // Activation rules:
      //   - 3+ tool calls in the last turn → new chain or continuation
      //   - 1-2 tool calls AND we're already mid-chain → continuation only
      //   - 0 tool calls → clear the chain
      const lastAssistant = messages.filter(m => m.role === "assistant").slice(-1);
      const lastTurnToolCount = lastAssistant.reduce((n, m) => n + countToolCalls(m), 0);
      const existingChain = toolChainIds.get(sessionId) ?? [];
      const lastTurnHadTools =
        lastTurnToolCount >= 3 ||
        (lastTurnToolCount >= 1 && existingChain.length > 0);
      const pendingChainIds = lastTurnHadTools ? existingChain : [];

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

    async afterTurn({ sessionId, messages, prePromptMessageCount }): Promise<void> {
      if (!readGraphMode()) return;

      // Only ingest messages from THIS turn — not the full session history.
      // prePromptMessageCount tells us how many messages existed before the prompt
      // was sent; everything after that index is new this turn.
      // This is critical: without this slice, every message gets tagged with
      // every topic ever discussed in the session (mega-tag contamination).
      const turnMessages = messages.slice(prePromptMessageCount);

      // Ingest new assistant messages from this turn
      const assistantMessages = turnMessages.filter((m) => m.role === "assistant");
      if (assistantMessages.length === 0) return;

      const userMessages = turnMessages.filter((m) => m.role === "user");
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

      // Detect tool use in this turn.
      // Only grow the chain if this turn had 3+ tool calls (starts or extends a chain)
      // OR had 1-2 calls while already mid-chain (continues an established chain).
      // Turns with zero tool calls implicitly clear the chain by not appending.
      const turnToolCount = assistantMessages.reduce((n, m) => n + countToolCalls(m), 0);
      const existingIds = toolChainIds.get(sessionId) ?? [];
      const hadTools =
        turnToolCount >= 3 ||
        (turnToolCount >= 1 && existingIds.length > 0);
      if (hadTools) {
        existingIds.push(turnId);
        toolChainIds.set(sessionId, existingIds.slice(-10));
      } else {
        // Non-qualifying turn — clear the chain so stale state doesn't persist
        toolChainIds.delete(sessionId);
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

  // Register /tags command
  api.registerCommand({
    name: "tags",
    description: "View and manage context graph tags (system + user)",
    acceptsArgs: true,
    handler: async (ctx) => {
      const args = (ctx.args ?? "").trim();
      const parts = args.split(/\s+/).filter(Boolean);
      const subcommand = parts[0]?.toLowerCase() ?? "";

      // Infer channel label from sender/session context
      const channelLabel = inferChannelLabel(ctx.senderId, (ctx as any).sessionId, logger);

      // ── /tags (no args) — overview ───────────────────────────────
      if (!subcommand) {
        const [tagsData, qualityData] = await Promise.all([
          apiGet(`/tags?channel_label=${channelLabel}`, logger),
          apiGet("/quality", logger),
        ]);

        if (!tagsData) {
          return { text: "❌ Context graph API unreachable." };
        }

        const { system_tags, user_tags } = tagsData as {
          system_tags: Array<{ name: string; state: string; hits: number; corpus_pct?: number }>;
          user_tags: Array<{ name: string; state: string; hits: number }>;
        };

        const core = system_tags
          .filter((t) => t.state === "core" && t.hits > 0)
          .sort((a, b) => b.hits - a.hits);
        const userActive = user_tags.filter((t) => t.state !== "archived");
        const userArchived = user_tags.filter((t) => t.state === "archived");

        const q = qualityData as {
          tag_entropy?: number;
          zero_return_rate?: number;
          corpus_size?: number;
        } | null;

        const lines: string[] = [];
        lines.push("**📊 Context Graph Tags**");
        lines.push("");
        lines.push(
          `**${core.length}** system core · **${userActive.length}** user` +
            (userArchived.length ? ` · ${userArchived.length} archived` : "") +
            (q
              ? ` · entropy ${q.tag_entropy?.toFixed(2)} · zero-return ${((q.zero_return_rate ?? 0) * 100).toFixed(1)}% · corpus ${q.corpus_size}`
              : "")
        );
        lines.push("");

        // Top 10 system tags
        lines.push("**System (top 10):**");
        for (const t of core.slice(0, 10)) {
          lines.push(`• \`${t.name}\` — ${t.hits.toLocaleString()}`);
        }
        if (core.length > 10) {
          lines.push(`• _(+${core.length - 10} more)_`);
        }
        lines.push("");

        // User tags
        if (userActive.length > 0 || userArchived.length > 0) {
          lines.push(`**User (\`${channelLabel}\`):**`);
          for (const t of userActive) {
            lines.push(`• \`${t.name}\` — ${t.hits}`);
          }
          for (const t of userArchived) {
            lines.push(`• ~~\`${t.name}\`~~ — archived`);
          }
        } else {
          lines.push(`_No user tags for \`${channelLabel}\`._`);
        }

        lines.push("");
        lines.push(
          "Commands: `/tags system` · `/tags user` · `/tags user add <name>` · `/tags user del <name>`"
        );

        return { text: lines.join("\n") };
      }

      // ── /tags system ─────────────────────────────────────────────
      if (subcommand === "system") {
        const data = await apiGet("/tags/system", logger);
        if (!data) {
          return { text: "❌ Context graph API unreachable." };
        }

        const { tags } = data as {
          tags: Array<{ name: string; state: string; hits: number; corpus_pct?: number }>;
        };

        const core = tags
          .filter((t) => t.state === "core")
          .sort((a, b) => b.hits - a.hits);
        const emerging = tags.filter((t) => t.state === "emerging");
        const archived = tags.filter((t) => t.state === "archived");

        const lines: string[] = [];
        lines.push(`**🏷️ System Tags** (${core.length} core, ${emerging.length} emerging, ${archived.length} archived)`);
        lines.push("");

        for (const t of core) {
          const pct = t.corpus_pct != null ? ` (${(t.corpus_pct * 100).toFixed(1)}%)` : "";
          lines.push(`• \`${t.name}\` — ${t.hits.toLocaleString()}${pct}`);
        }

        if (emerging.length > 0) {
          lines.push("");
          lines.push("**Emerging:**");
          for (const t of emerging) {
            lines.push(`• \`${t.name}\` — ${t.hits}`);
          }
        }

        return { text: lines.join("\n") };
      }

      // ── /tags user [add|del] ─────────────────────────────────────
      if (subcommand === "user") {
        const action = parts[1]?.toLowerCase() ?? "";
        const tagName = parts.slice(2).join("-").toLowerCase();

        // /tags user add <name>
        if (action === "add") {
          if (!tagName) {
            return { text: "Usage: `/tags user add <tag-name>`" };
          }

          const result = await apiPost(
            `/tags/user/${channelLabel}/add`,
            { name: tagName },
            logger
          );

          if (!result) {
            return { text: `❌ Failed to add user tag \`${tagName}\`. API error.` };
          }

          const r = result as { status: string; tag?: { name: string; keywords?: string[] }; error?: string };
          if (r.status === "error" || r.error) {
            return { text: `❌ ${r.error || "Failed to add tag."}` };
          }

          const kw = r.tag?.keywords?.length
            ? `\nKeywords: ${r.tag.keywords.map((k: string) => `\`${k}\``).join(", ")}`
            : "";
          return {
            text: `✅ Added user tag \`${tagName}\` for \`${channelLabel}\`.${kw}\n\nRun \`/tags user retag\` to backfill existing messages.`,
          };
        }

        // /tags user del <name>
        if (action === "del" || action === "delete" || action === "rm") {
          if (!tagName) {
            return { text: "Usage: `/tags user del <tag-name>`" };
          }

          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
          try {
            const res = await fetch(
              `${PYTHON_API_BASE}/tags/user/${channelLabel}/${tagName}`,
              { method: "DELETE", signal: controller.signal }
            );
            clearTimeout(timer);

            if (!res.ok) {
              const body = await res.text();
              return { text: `❌ Failed to archive \`${tagName}\`: ${body}` };
            }

            return { text: `🗑️ Archived user tag \`${tagName}\`.` };
          } catch (err: unknown) {
            clearTimeout(timer);
            const msg = err instanceof Error ? err.message : String(err);
            return { text: `❌ API error: ${msg}` };
          }
        }

        // /tags user retag
        if (action === "retag") {
          const result = await apiPost(`/tags/user/${channelLabel}/retag`, {}, logger);
          if (!result) {
            return { text: "❌ Retag failed — API error." };
          }
          const r = result as { retagged?: number; status?: string };
          return {
            text: `🔄 Retagged ${r.retagged ?? 0} messages with user tags for \`${channelLabel}\`.`,
          };
        }

        // /tags user (list)
        const data = await apiGet(`/tags/user/${channelLabel}`, logger);
        if (!data) {
          return { text: "❌ Context graph API unreachable." };
        }

        const { tags } = data as {
          tags: Array<{ name: string; state: string; hits: number; keywords?: string[] }>;
        };

        if (tags.length === 0) {
          return {
            text: `_No user tags for \`${channelLabel}\`._\n\nAdd one: \`/tags user add <name>\``,
          };
        }

        const lines: string[] = [];
        lines.push(`**👤 User Tags** (\`${channelLabel}\`)`);
        lines.push("");

        for (const t of tags) {
          const state = t.state === "archived" ? " _(archived)_" : "";
          const kw =
            t.keywords?.length ? ` — keywords: ${t.keywords.join(", ")}` : "";
          lines.push(`• \`${t.name}\` — ${t.hits} hits${kw}${state}`);
        }

        lines.push("");
        lines.push(
          "Commands: `/tags user add <name>` · `/tags user del <name>` · `/tags user retag`"
        );

        return { text: lines.join("\n") };
      }

      return {
        text: `Unknown subcommand: \`${subcommand}\`\n\nUsage:\n• \`/tags\` — overview\n• \`/tags system\` — system tags\n• \`/tags user\` — user tags\n• \`/tags user add <name>\`\n• \`/tags user del <name>\``,
      };
    },
  });

  logger.info("contextgraph: plugin ready (default: graph mode OFF)");
}
