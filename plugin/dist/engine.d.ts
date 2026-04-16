/**
 * ContextEngine implementation for contextgraph plugin
 *
 * This is a SWITCHABLE engine that can operate in two modes:
 * - OFF: pass-through (returns messages as-is, like LegacyContextEngine)
 * - ON: calls Python API for graph-based assembly
 */
import type { ContextEngine, AssembleResult, CompactResult, IngestResult, ContextEngineInfo } from "openclaw/plugin-sdk/context-engine";
import type { AgentMessage } from "@mariozechner/pi-agent-core";
export declare class ContextGraphEngine implements ContextEngine {
    readonly info: ContextEngineInfo;
    private client;
    private stateDir;
    private stateFile;
    private comparisonLogFile;
    constructor();
    private loadState;
    private saveState;
    private isGraphModeEnabled;
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
    private inferChannelLabel;
    private detectToolChains;
    private extractMessageText;
    private estimateTokens;
    ingest(params: {
        sessionId: string;
        message: AgentMessage;
        isHeartbeat?: boolean;
    }): Promise<IngestResult>;
    assemble(params: {
        sessionId: string;
        messages: AgentMessage[];
        tokenBudget?: number;
    }): Promise<AssembleResult>;
    compact(): Promise<CompactResult>;
    afterTurn(params: {
        sessionId: string;
        sessionFile: string;
        messages: AgentMessage[];
        prePromptMessageCount: number;
        autoCompactionSummary?: string;
        isHeartbeat?: boolean;
        tokenBudget?: number;
    }): Promise<void>;
    dispose(): Promise<void>;
}
