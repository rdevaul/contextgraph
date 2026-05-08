/**
 * HTTP client for contextgraph Python API
 */
export class ContextGraphAPIClient {
    baseURL;
    constructor(baseURL) {
        this.baseURL = baseURL ?? process.env.CONTEXTGRAPH_API_URL ?? "http://127.0.0.1:8302";
    }
    async tag(userText, assistantText) {
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
    async ingest(message) {
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
    async assemble(userText, tags, tokenBudget = 4000, toolState, options) {
        // Per bus thread 20260501213940-5b002851 + approval 20260501220916-a4feb6f0:
        // session_id, channel_label, user_tags, and scope must be threaded through
        // so the Python assembler can scope retrieval. Without these every assemble
        // call retrieves globally across the entire store, which causes cross-pane
        // and cross-user content bleed.
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
                ...(options?.scope ? { scope: options.scope } : {}),
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
                scope: options?.scope ?? null,
            }));
            throw new Error(`Assemble request failed: ${response.statusText} — ${errorBody}`);
        }
        return await response.json();
    }
    async compare(userText, assistantText) {
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
    async health() {
        const response = await fetch(`${this.baseURL}/health`);
        if (!response.ok) {
            throw new Error(`Health check failed: ${response.statusText}`);
        }
        return await response.json();
    }
}
