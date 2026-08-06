# test_tools.py  (throwaway)
from dotenv import load_dotenv
load_dotenv()

from graph.tools import search_internal_context, web_search

print("--- internal ---")
for e in search_internal_context("do users actually want summaries?"):
    print(f"[{e.source}] {e.content[:80]}...")

print("\n--- web ---")
for e in web_search("AI note summarization feature adoption"):
    print(f"[{e.source}] {e.content[:80]}... ({e.url})")