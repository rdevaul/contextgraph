/**
 * HTTP client for contextgraph Python API
 */
export interface TagResponse {
    tags: string[];
    confidence: number;
    per_tagger: Record<string, string[]>;
}
export interface IngestResponse {
    ingested: boolean;
    tags: string[];
}
export interface AssembleMessage {
    id: string;
    user_text: string;
    assistant_text: string;
    tags: string[];
    timestamp: number;
}
export interface AssembleResponse {
    messages: AssembleMessage[];
    total_tokens: number;
    recency_count: number;
    topic_count: number;
    sticky_count: number;
    tags_used: string[];
}
export interface ToolState {
    last_turn_had_tools: boolean;
    pending_chain_ids: string[];
}
export interface CompareResponse {
    graph_assembly: {
        messages: AssembleMessage[];
        total_tokens: number;
        recency_count: number;
        topic_count: number;
        tags_used: string[];
    };
    linear_window: {
        messages: AssembleMessage[];
        total_tokens: number;
        recency_count: number;
        topic_count: number;
        tags_used: string[];
    };
}
export interface HealthResponse {
    status: string;
    messages_in_store: number;
    tags: string[];
    engine: string;
}
export declare class ContextGraphAPIClient {
    private baseURL;
    constructor(baseURL?: string);
    tag(userText: string, assistantText: string): Promise<TagResponse>;
    ingest(message: {
        id?: string;
        session_id: string;
        user_text: string;
        assistant_text: string;
        timestamp: number;
        user_id?: string;
        external_id?: string;
        channel_label?: string;
    }): Promise<IngestResponse>;
    assemble(userText: string, tags?: string[], tokenBudget?: number, toolState?: ToolState): Promise<AssembleResponse>;
    compare(userText: string, assistantText: string): Promise<CompareResponse>;
    health(): Promise<HealthResponse>;
}
