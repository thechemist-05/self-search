# src/api/app.py
"""FastAPI backend for Self Search.

Endpoints:
- POST /chat          – Conversational product discovery endpoint (Phase 7). Searches are
                        product-first: no model call.
- POST /chat/summary  – Optional model summary of the latest search results.
- GET  /sessions/{id} – Retrieve conversation session state.
- DELETE /sessions/{id} – Clear a conversation session.
- GET  /health        – System health check (database, Ollama, session count).
- POST /ask           – Legacy answer endpoint using LLM agent.
- POST /populate      – Rebuild the product/review indexes (validated, atomic swap).

All conversational and data endpoints support optional API-key authentication
via the ``X-API-Key`` header. There are no user profiles (removed 2026-10-21).
"""

from __future__ import annotations

import hmac
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, List, Literal, Optional

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from scripts.generate_embeddings import rebuild_indexes
from src.agent.controller import (
    AgentController,
    InvalidProductSelectionError,
    NothingToSummariseError,
    SessionNotFoundError,
)
from src.agent.schemas import AgentResponse
from src.config.logging import make_console_output_safe
from src.config.settings import get_settings
from src.db.connection import SerializedConnection
from src.evidence.validator import EvidenceValidator
from src.llm.agent import answer_query
from src.llm.ollama import OllamaProvider
from src.llm.service import LLMService
from src.retrieval.hybrid import HybridRetriever
from src.retrieval.retrieval import retrieve
from src.vector_store.manifest import IndexManifestError, require_valid_manifest
from src.vector_store.vector_store import vector_store_dir

logger = structlog.get_logger("shelfsense.api")
make_console_output_safe()  # a "\u20b9" in a logged query must not turn into HTTP 500 on Windows


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Build the controller at startup, so a database/index mismatch stops the server
    instead of serving wrong products. A controller injected by tests is used as is."""
    get_agent_controller()
    yield


app = FastAPI(title="Self Search API", version="0.2.0", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Security (optional API key)
# ---------------------------------------------------------------------------
_API_KEY = os.getenv("API_KEY")


def verify_api_key(x_api_key: Optional[str] = Header(None)) -> bool:
    if _API_KEY:
        # Constant-time comparison so response timing does not leak the key.
        if x_api_key is None or not hmac.compare_digest(x_api_key.encode(), _API_KEY.encode()):
            raise HTTPException(status_code=401, detail="Invalid API key")
    return True


# ---------------------------------------------------------------------------
# Dependency Injection for Agent Controller
# ---------------------------------------------------------------------------
_agent_controller: Optional[AgentController] = None


def open_api_connection(db_file: Path) -> sqlite3.Connection:
    """Open the SQLite connection shared by the API's agent controller.

    FastAPI runs sync endpoints in a worker threadpool, so a connection bound to
    its creating thread (the sqlite3 default) fails on every later request.

    The connection is a ``SerializedConnection``: one re-entrant lock covers each
    statement including fetching its rows, and explicit transactions hold the
    same lock end to end. Without this, overlapping requests got wrong
    or empty results (found in Phase 9; see docs/phase-reports/phase-9-report.md).
    """
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file), check_same_thread=False, factory=SerializedConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def create_default_agent_controller() -> AgentController:
    """Build production AgentController wiring db, retriever, LLM, and validator."""
    settings = get_settings()
    conn = open_api_connection(Path(settings.db_path))
    # FAISS ids are database row ids: refuse to serve indexes built from another catalogue.
    index_dir = vector_store_dir()
    try:
        require_valid_manifest(conn, index_dir)
    except IndexManifestError as exc:
        logger.error("vector_index_manifest_invalid", index_dir=str(index_dir), db_path=settings.db_path,
                     problems=exc.problems,
                     fix="python scripts/verify_index_manifest.py --check, or rebuild with "
                         "python scripts/generate_embeddings.py --overwrite")
        conn.close()
        raise
    llm_provider = OllamaProvider()
    retriever = HybridRetriever(conn=conn)
    llm_service = LLMService(provider=llm_provider)
    validator = EvidenceValidator(conn=conn)

    return AgentController(
        conn=conn,
        llm_provider=llm_provider,
        retriever=retriever,
        llm_service=llm_service,
        validator=validator,
    )


def get_agent_controller() -> AgentController:
    """Retrieve or lazily initialize the singleton AgentController."""
    global _agent_controller
    if _agent_controller is None:
        _agent_controller = create_default_agent_controller()
    return _agent_controller


def set_agent_controller(controller: Optional[AgentController]) -> None:
    """Explicitly set or reset the AgentController instance (useful for tests)."""
    global _agent_controller
    _agent_controller = controller


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="User message or query.")
    session_id: Optional[str] = Field(None, description="Optional conversation session ID.")
    product_ids: Optional[List[int]] = Field(
        None,
        description="Optional explicit product selection (Phase 9): 1 ID to ask about that "
        "product, 2-4 IDs to compare. Every ID must have been shown in this session.",
    )


class SummaryRequest(BaseModel):
    session_id: str = Field(..., min_length=1, description="Session whose latest search results to summarise.")
    result_product_ids: Optional[List[int]] = Field(
        None,
        description="The results the client is showing (result_product_ids of the search response). "
        "When given they must equal the session's latest results; otherwise 409.",
    )


class AskRequest(BaseModel):
    query: str = Field(..., description="User question or search phrase.")
    k: int = Field(5, ge=1, le=20, description="Number of evidence items to retrieve.")


class EvidenceItem(BaseModel):
    entity_type: str = Field(..., description='"product" or "review".')
    entity_id: int = Field(..., description="SQLite row ID of the entity.")
    distance: float = Field(..., description="FAISS distance metric.")


class AskResponse(BaseModel):
    answer: str = Field(..., description="LLM‑generated answer.")
    evidence: List[EvidenceItem] = Field(..., description="Retrieved evidence items.")


class PopulateResponse(BaseModel):
    status: str = Field(..., description="Result of the embedding generation.")


# ---------------------------------------------------------------------------
# Phase 7 Conversational Endpoints
# ---------------------------------------------------------------------------
@app.post("/chat", response_model=AgentResponse, dependencies=[Depends(verify_api_key)])
def chat(
    request: ChatRequest,
    controller: AgentController = Depends(get_agent_controller),
) -> AgentResponse:
    """Main conversational endpoint for Self Search.

    Parses user query, routes intent, executes retrieval, generates answer,
    validates evidence, and maintains conversational session state.
    """
    try:
        return controller.handle_message(
            user_message=request.message,
            session_id=request.session_id,
            product_ids=request.product_ids,
        )
    except InvalidProductSelectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/chat/summary", response_model=AgentResponse, dependencies=[Depends(verify_api_key)])
def chat_summary(
    request: SummaryRequest,
    controller: AgentController = Depends(get_agent_controller),
) -> AgentResponse:
    """Optional, evidence-grounded model summary of the session's latest search results.

    ``/chat`` searches return their products without calling the model; this endpoint
    is the only way a search gets a model summary. The response has the same products
    in the same order as the search, plus the validated summary and claims.
    404: unknown session. 409: no results, or the given results are no longer the latest.
    """
    try:
        return controller.summarise_results(request.session_id, request.result_product_ids)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except NothingToSummariseError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/sessions/{session_id}", dependencies=[Depends(verify_api_key)])
def get_session(
    session_id: str,
    controller: AgentController = Depends(get_agent_controller),
) -> dict[str, Any]:
    """Retrieve state and history for an active session."""
    session = controller.session_manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return session.to_dict()


@app.delete("/sessions/{session_id}", dependencies=[Depends(verify_api_key)])
def delete_session(
    session_id: str,
    controller: AgentController = Depends(get_agent_controller),
) -> dict[str, str]:
    """Clear a conversation session."""
    success = controller.session_manager.delete(session_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return {"status": "deleted", "session_id": session_id}


@app.delete("/sessions/{session_id}/filters/{name}", dependencies=[Depends(verify_api_key)])
def clear_session_filter(
    session_id: str,
    name: Literal["category", "brand", "min_price", "max_price", "min_rating"],
    controller: AgentController = Depends(get_agent_controller),
) -> dict[str, Any]:
    """Remove one active filter from a session (in memory only; nothing is persisted).

    Takes effect on the next search or refinement in this session.
    """
    session = controller.session_manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    session.clear_filter(name)
    return {"session_id": session_id, "active_filters": session.to_dict()["active_filters"]}


@app.get("/products/{product_id}", dependencies=[Depends(verify_api_key)])
def get_product(
    product_id: int,
    controller: AgentController = Depends(get_agent_controller),
) -> dict[str, Any]:
    """Product details and locally stored reviews, read from the database only."""
    detail = controller.presenter.detail(product_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Product {product_id} not found")
    return detail


@app.get("/health")
def health(
    controller: AgentController = Depends(get_agent_controller),
) -> dict[str, Any]:
    """Health check endpoint reporting subsystem availability."""
    db_alive = False
    try:
        controller.conn.execute("SELECT 1").fetchone()
        db_alive = True
    except Exception:
        db_alive = False

    ollama_alive = controller.llm_provider.is_available()

    return {
        "status": "ok",
        "database": "connected" if db_alive else "disconnected",
        "ollama": "available" if ollama_alive else "unavailable",
        "active_sessions": controller.session_manager.count,
    }


# ---------------------------------------------------------------------------
# Legacy Endpoints (Preserved for Backward Compatibility)
# ---------------------------------------------------------------------------
@app.post("/ask", response_model=AskResponse, dependencies=[Depends(verify_api_key)])
def ask(request: AskRequest) -> AskResponse:
    evidence_raw = retrieve(request.query, k=request.k)
    evidence = [
        EvidenceItem(entity_type=et, entity_id=eid, distance=dist)
        for et, eid, dist in evidence_raw
    ]
    answer = answer_query(request.query, k=request.k)
    return AskResponse(answer=answer, evidence=evidence)


@app.post("/populate", response_model=PopulateResponse, dependencies=[Depends(verify_api_key)])
def populate() -> PopulateResponse:
    try:
        rebuild_indexes(db_path=Path(get_settings().db_path), overwrite=True)
        return PopulateResponse(status="embeddings generated successfully")
    except Exception as exc:  # IndexBuildError and anything unexpected: indexes left unchanged
        raise HTTPException(status_code=500, detail=str(exc))
