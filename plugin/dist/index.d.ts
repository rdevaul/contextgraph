/**
 * contextgraph — OpenClaw ContextEngine Plugin
 *
 * Bridges OpenClaw's ContextEngine interface to the contextgraph Python
 * FastAPI server running at http://localhost:8302.
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
export default function register(api: OpenClawPluginApi): void;
