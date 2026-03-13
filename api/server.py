import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from store import MessageStore, Message
from features import extract_features
from tagger import assign_tags
from ensemble import EnsembleTagger
from assembler import ContextAssembler
from quality import QualityAgent
from gp_tagger import GeneticTagger
import pickle
import os

app = FastAPI()

class TagRequest(BaseModel):
    user_text: str
    assistant_text: str

class IngestRequest(BaseModel):
    id: str = Field(None, nullable=True)
    session_id: str
    user_text: str
    assistant_text: str
    timestamp: float
    user_id: str = Field(None, nullable=True)

class AssembleRequest(BaseModel):
    user_text: str
    tags: list[str] = Field(None, nullable=True)
    token_budget: int = 4000

class CompareResponse(BaseModel):
    graph_assembly: dict
    linear_window: dict

store = MessageStore()
quality_agent = QualityAgent()
ensemble = EnsembleTagger(quality_agent=quality_agent)

gp_tagger_path = Path(__file__).parent.parent / 'data' / 'gp-tagger.pkl'
if gp_tagger_path.exists():
    with open(gp_tagger_path, 'rb') as f:
        gp_tagger = pickle.load(f)
        ensemble.register(gp_tagger.tagger_id, gp_tagger.assign, 1.0)

baseline_tagger = lambda features, user_text, assistant_text: assign_tags(features, user_text, assistant_text)
ensemble.register('baseline', baseline_tagger, 1.0)

@app.on_event("startup")
async def startup_event():
    store.get_all_tags()  # Initialize the store

@app.post("/tag", response_model=dict)
def tag(request: TagRequest):
    try:
        features = extract_features(request.user_text, request.assistant_text)
        result = ensemble.assign(features, request.user_text, request.assistant_text)
        return {"tags": result.tags, "confidence": result.confidence, "per_tagger": result.per_tagger}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/ingest", response_model=dict)
def ingest(request: IngestRequest):
    try:
        message_id = request.id if request.id else f"api-{time.time()}"
        features = extract_features(request.user_text, request.assistant_text)
        tags = ensemble.assign(features, request.user_text, request.assistant_text).tags
        message = Message(
            id=message_id,
            session_id=request.session_id,
            user_text=request.user_text,
            assistant_text=request.assistant_text,
            timestamp=request.timestamp,
            user_id=request.user_id or "default",
            tags=tags,
            token_count=len(request.user_text.split()) + len(request.assistant_text.split())
        )
        store.add_message(message)
        return {"ingested": True, "tags": tags}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/assemble", response_model=dict)
def assemble(request: AssembleRequest):
    try:
        features = extract_features(request.user_text, "")  # Empty assistant_text for incoming message
        if not request.tags:
            request.tags = ensemble.assign(features, request.user_text, "").tags
        assembler = ContextAssembler(store, token_budget=request.token_budget)
        result = assembler.assemble(request.user_text, request.tags)
        return {
            "messages": [{"id": msg.id, "user_text": msg.user_text, "assistant_text": msg.assistant_text, "tags": msg.tags, "timestamp": msg.timestamp} for msg in result.messages],
            "total_tokens": result.total_tokens,
            "recency_count": result.recency_count,
            "topic_count": result.topic_count,
            "tags_used": result.tags_used
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health", response_model=dict)
def health():
    try:
        messages_in_store = len(store.get_recent(1000))  # Approximate count
        tags = store.get_all_tags()
        return {"status": "ok", "messages_in_store": messages_in_store, "tags": tags, "engine": "contextgraph"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/metrics", response_model=dict)
def metrics():
    try:
        # Build quality stats dict from all tagger IDs
        quality_stats = {}
        for tagger_id in quality_agent.all_tagger_ids():
            stats = quality_agent.stats(tagger_id)
            if stats:
                quality_stats[tagger_id] = {
                    "fitness": quality_agent.fitness(tagger_id),
                    "mean_density": stats.mean_density(),
                    "mean_reframing": stats.mean_reframing()
                }

        # Build tagger fitness from ensemble
        tagger_fitness = {}
        for entry in ensemble._taggers:
            tagger_fitness[entry.tagger_id] = entry.weight

        return {"quality_stats": quality_stats, "tagger_fitness": tagger_fitness}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/compare", response_model=CompareResponse)
def compare(request: TagRequest):
    try:
        features = extract_features(request.user_text, request.assistant_text)
        inferred_tags = ensemble.assign(features, request.user_text, request.assistant_text).tags

        # Graph Assembly
        assembler = ContextAssembler(store, token_budget=4000)
        graph_assembly_result = assembler.assemble(request.user_text, inferred_tags)
        graph_assembly = {
            "messages": [{"id": msg.id, "user_text": msg.user_text, "assistant_text": msg.assistant_text, "tags": msg.tags, "timestamp": msg.timestamp} for msg in graph_assembly_result.messages],
            "total_tokens": graph_assembly_result.total_tokens,
            "recency_count": graph_assembly_result.recency_count,
            "topic_count": graph_assembly_result.topic_count,
            "tags_used": graph_assembly_result.tags_used
        }

        # Simulated Linear Window — pack to 4000 token budget, newest-first
        linear_window_messages = []
        linear_tokens = 0
        budget = 4000

        for msg in store.get_recent(100):  # Fetch enough to fill budget
            msg_tokens = len(msg.user_text.split()) + len(msg.assistant_text.split())
            if linear_tokens + msg_tokens > budget:
                break
            linear_window_messages.append(msg)
            linear_tokens += msg_tokens

        # Reverse to oldest-first for consistency with graph assembly
        linear_window_messages.reverse()

        linear_window = {
            "messages": [{"id": msg.id, "user_text": msg.user_text, "assistant_text": msg.assistant_text, "tags": msg.tags, "timestamp": msg.timestamp} for msg in linear_window_messages],
            "total_tokens": linear_tokens,
            "recency_count": len(linear_window_messages),
            "topic_count": len(set(tag for msg in linear_window_messages for tag in msg.tags)),
            "tags_used": list(set(tag for msg in linear_window_messages for tag in msg.tags))
        }

        return CompareResponse(graph_assembly=graph_assembly, linear_window=linear_window)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8300)
