import os
import requests
from pydantic import BaseModel
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import Chroma
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
# Closed-world: internal context retriever (PRD + feedback)
# ---------------------------------------------------------------

_retriever = None  # module-level cache so we embed only once

def _build_retriever():
    global _retriever
    if _retriever is not None:
        return _retriever

    docs = []
    for path in ["context/prd.md", "context/feedback.md"]:
        loaded = TextLoader(path).load()
        for d in loaded:
            d.metadata["source"] = os.path.basename(path)
        docs.extend(loaded)

    chunks = RecursiveCharacterTextSplitter(
        chunk_size=500, chunk_overlap=50
    ).split_documents(docs)

    store = Chroma.from_documents(chunks, OpenAIEmbeddings())
    _retriever = store.as_retriever(search_kwargs={"k": 3})
    return _retriever

@traceable(run_type="tool")
def search_internal_context(query: str) -> list[Evidence]:
    """Search the product's own PRD + user feedback.
    Use for anything about THIS product, its users, or the
    proposed feature. Returns up to 3 relevant chunks."""
    hits = _build_retriever().invoke(query)
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
    """Search the web (Google via Serper) for market data,
    competitors, or external evidence NOT in the internal docs.
    Returns up to k results as {title, snippet, url}."""
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
# ---------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web (Google) for market data, competitors, industry "
                "trends, or external evidence NOT found in the internal docs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"],
            },
        },
    },
]

# name -> callable, for executing whatever the model asks for
TOOL_DISPATCH = {
    "search_internal_context": search_internal_context,
    "web_search": web_search,
}