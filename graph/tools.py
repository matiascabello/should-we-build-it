import os
import uuid
import contextvars
from typing import Optional
import requests
from pydantic import BaseModel
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStoreRetriever
from langchain_community.document_loaders import TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langsmith import traceable


class Evidence(BaseModel):
    """Uniform return shape for BOTH tools, so the fact-checker
    and UI can treat internal and web evidence identically."""
    source: str      # doc name (internal) or page title (web)
    content: str     # the retrieved chunk / snippet
    url: str = ""    # populated for web results, empty for internal


# ---------------------------------------------------------------
# Closed-world: internal context retriever (per-request context,
# falling back to the default PRD + feedback docs)
# ---------------------------------------------------------------

def build_retriever_from_docs(docs: list[tuple[str, str]]) -> Optional[VectorStoreRetriever]:
    """Build a fresh retriever over (title, content) pairs.

    Each call gets its own Chroma collection (a unique collection_name)
    rather than the client's default one. Chroma's default in-memory
    client is process-wide and shared across every Chroma(...) instance
    with no persist_directory — reusing the default collection name would
    make unrelated requests' documents pile into the same collection.
    """
    documents = [
        Document(page_content=content.strip(), metadata={"source": title or f"context-{i+1}"})
        for i, (title, content) in enumerate(docs)
        if content and content.strip()
    ]
    if not documents:
        return None

    chunks = RecursiveCharacterTextSplitter(
        chunk_size=500, chunk_overlap=50
    ).split_documents(documents)

    store = Chroma.from_documents(
        chunks, OpenAIEmbeddings(), collection_name=f"debate-{uuid.uuid4().hex}"
    )
    return store.as_retriever(search_kwargs={"k": 3})


_default_retriever: Optional[VectorStoreRetriever] = None  # cache: default docs, built once

def _get_default_retriever() -> Optional[VectorStoreRetriever]:
    """Fallback retriever over the fixed context/*.md files, for callers
    (e.g. test_tools.py) that run outside a request context."""
    global _default_retriever
    if _default_retriever is not None:
        return _default_retriever

    docs = []
    for path in ["context/prd.md", "context/feedback.md"]:
        loaded = TextLoader(path).load()
        for d in loaded:
            docs.append((os.path.basename(path), d.page_content))

    _default_retriever = build_retriever_from_docs(docs)
    return _default_retriever


# Request-scoped retriever: set per-request so search_internal_context
# searches THAT run's context, without threading extra params through
# graph/nodes.py's agent loop. Isolated per asyncio task, so concurrent
# debates never see each other's context.
_retriever_ctx: contextvars.ContextVar[Optional[VectorStoreRetriever]] = (
    contextvars.ContextVar("active_retriever", default=None)
)

def set_active_retriever(retriever: Optional[VectorStoreRetriever]) -> contextvars.Token:
    return _retriever_ctx.set(retriever)

def clear_active_retriever(token: contextvars.Token) -> None:
    _retriever_ctx.reset(token)


@traceable(run_type="tool")
def search_internal_context(query: str) -> list[Evidence]:
    """Search the product's own context docs (the current request's
    supplied context, or the default PRD + feedback if none was set).
    Use for anything about THIS product, its users, or the
    proposed feature. Returns up to 3 relevant chunks."""
    retriever = _retriever_ctx.get() or _get_default_retriever()
    if retriever is None:
        return []
    hits = retriever.invoke(query)
    return [
        Evidence(
            source=h.metadata.get("source", "internal"),
            content=h.page_content.strip(),
        )
        for h in hits
    ]


# ---------------------------------------------------------------
# Open-world: web search via Serper
# ---------------------------------------------------------------

@traceable(run_type="tool")
def web_search(query: str, k: int = 3) -> list[Evidence]:
    """Search the web (Google via Serper) for evidence outside this
    product's own docs: competitor/industry precedent, adoption
    benchmarks, market sizing. Returns up to k results as
    {title, snippet, url}."""
    resp = requests.post(
        "https://google.serper.dev/search",
        headers={
            "X-API-KEY": os.environ["SERPER_API_KEY"],
            "Content-Type": "application/json",
        },
        json={"q": query},
        timeout=15,
    )
    resp.raise_for_status()
    organic = resp.json().get("organic", [])[:k]
    return [
        Evidence(
            source=item.get("title", "web"),
            content=item.get("snippet", "").strip(),
            url=item.get("link", ""),
        )
        for item in organic
    ]

# ---------------------------------------------------------------
# Tool registry: schemas for the model + dispatch for execution
#
# Flat shape (name/description/parameters at the top level, not nested
# under "function") because graph/nodes.py calls the Responses API
# (client.responses.create), not Chat Completions — the two APIs use
# different function-tool schema shapes.
# ---------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "name": "search_internal_context",
        "description": (
            "Search the product's own PRD and user feedback. Use for "
            "anything about THIS product, its users, the proposed feature, "
            "costs, or scope. Returns up to 3 relevant chunks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look up"}
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "web_search",
        "description": (
            "Search the web (Google) for evidence the internal docs can't "
            "provide because it lives outside this product: how competitors "
            "or similar products handle this, industry adoption/success "
            "benchmarks for comparable features, published failure or "
            "backlash stories, market sizing. Use this to test whether an "
            "internal assumption holds up against outside evidence, not "
            "just when internal search comes up empty."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"}
            },
            "required": ["query"],
        },
    },
]

# name -> callable, for executing whatever the model asks for
TOOL_DISPATCH = {
    "search_internal_context": search_internal_context,
    "web_search": web_search,
}
