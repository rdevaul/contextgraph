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

export class ContextGraphAPIClient {
  private baseURL: string;

  constructor(baseURL?: string) {
    this.baseURL = baseURL ?? process.env.CONTEXTGRAPH_API_URL ?? "http://127.0.0.1:8302";
  }

  async tag(userText: string, assistantText: string): Promise<TagResponse> {
    const response = await fetch(`${this.baseURL}/tag`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_text: userText,
        assistant_text: assistantText,
      }),
    });

    if (!response.ok) {
      throw new Error(`Tag request failed: ${response.statusText}`);
    }

    return await response.json();
  }

  async ingest(message: {
    id?: string;
    session_id: string;
    user_text: string;
    assistant_text: string;
    timestamp: number;
    user_id?: string;
    external_id?: string;  // OpenClaw AgentMessage.id or other external system ID
    channel_label?: string;  // Channel label for per-user memory scoping
  }): Promise<IngestResponse> {
    const response = await fetch(`${this.baseURL}/ingest`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(message),
    });

    if (!response.ok) {
      throw new Error(`Ingest request failed: ${response.statusText}`);
    }

    return await response.json();
  }

  async assemble(
    userText: string,
    tags?: string[],
    tokenBudget: number = 4000,
    toolState?: ToolState,
    options?: {
      sessionId?: string;
      channelLabel?: string;
      userTags?: string[];
    }
  ): Promise<AssembleResponse> {
    // Part A (bus approval 20260501220916-a4feb6f0):
    // Thread session_id, channel_label, user_tags through so the Python
    // assembler can scope retrieval. Without these every assemble call
    // retrieves globally across the entire store — cross-user content bleed.
    const response = await fetch(`${this.baseURL}/assemble`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_text: userText,
        ...(tags && tags.length > 0 ? { tags } : {}),
        token_budget: tokenBudget,
        ...(toolState ? { tool_state: toolState } : {}),
        ...(options?.sessionId ? { session_id: options.sessionId } : {}),
        ...(options?.channelLabel ? { channel_label: options.channelLabel } : {}),
        ...(options?.userTags && options.userTags.length > 0 ? { user_tags: options.userTags } : {}),
      }),
    });

    if (!response.ok) {
      const errorBody = await response.text();
      console.error("[contextgraph] assemble 422 body:", errorBody);
      console.error("[contextgraph] assemble request body:", JSON.stringify({
        user_text: userText?.slice(0, 200),
        tags: tags || null,
        token_budget: tokenBudget,
        session_id: options?.sessionId ?? null,
        channel_label: options?.channelLabel ?? null,
      }));
      throw new Error(`Assemble request failed: ${response.statusText} — ${errorBody}`);
    }

    return await response.json();
  }

  async compare(
    userText: string,
    assistantText: string
  ): Promise<CompareResponse> {
    const response = await fetch(`${this.baseURL}/compare`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_text: userText,
        assistant_text: assistantText,
      }),
    });

    if (!response.ok) {
      throw new Error(`Compare request failed: ${response.statusText}`);
    }

    return await response.json();
  }

  async health(): Promise<HealthResponse> {
    const response = await fetch(`${this.baseURL}/health`);

    if (!response.ok) {
      throw new Error(`Health check failed: ${response.statusText}`);
    }

    return await response.json();
  }
}
