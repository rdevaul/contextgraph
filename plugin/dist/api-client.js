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
    async assemble(userText, tags, tokenBudget = 4000, toolState) {
        const response = await fetch(`${this.baseURL}/assemble`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                user_text: userText,
                ...(tags && tags.length > 0 ? { tags } : {}),
                token_budget: tokenBudget,
                ...(toolState ? { tool_state: toolState } : {}),
            }),
        });
        if (!response.ok) {
            const errorBody = await response.text();
            console.error("[contextgraph] assemble 422 body:", errorBody);
            console.error("[contextgraph] assemble request body:", JSON.stringify({ user_text: userText?.slice(0, 200), tags: tags || null, token_budget: tokenBudget }));
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
