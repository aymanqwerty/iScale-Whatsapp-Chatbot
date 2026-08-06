"""Knowledge base: loading JSON files and retrieving only what is relevant."""

from app.services.knowledge.loader import KnowledgeBase, KnowledgeLoader, load_knowledge_base
from app.services.knowledge.models import Course, KnowledgeSnippet
from app.services.knowledge.retriever import KeywordRetriever, KnowledgeRetriever

__all__ = [
    "Course",
    "KeywordRetriever",
    "KnowledgeBase",
    "KnowledgeLoader",
    "KnowledgeRetriever",
    "KnowledgeSnippet",
    "load_knowledge_base",
]
