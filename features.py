"""Feature extraction for tag-based LLM context management."""

from dataclasses import dataclass
from typing import List
import re

# Optional spacy import with graceful fallback
try:
    import spacy
    NLP = spacy.load("en_core_web_sm")
    SPACY_AVAILABLE = True
except (ImportError, Exception):
    SPACY_AVAILABLE = False

# Hardcoded stopword list for keyword extraction
STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for", "of",
    "with", "by", "from", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "shall", "can", "need", "dare",
    "ought", "used", "it", "its", "this", "that", "these", "those", "i",
    "you", "he", "she", "we", "they", "what", "which", "who", "whom",
    "whose", "where", "when", "why", "how", "all", "each", "every", "both",
    "few", "more", "most", "other", "some", "such", "no", "nor", "not",
    "only", "own", "same", "so", "than", "too", "very", "s", "t", "can",
    "don", "just", "now", "up", "down", "out", "off", "over", "under",
    "again", "further", "then", "once", "here", "there", "why", "how"
}

@dataclass
class MessageFeatures:
    """Features extracted from a user-assistant message pair."""
    token_count: int
    entities: List[str]
    noun_phrases: List[str]
    contains_code: bool
    contains_url: bool
    is_question: bool
    keywords: List[str]

def estimate_tokens(text: str) -> int:
    """Estimate token count using word count * 1.3."""
    words = re.findall(r"\b\w+\b", text)
    return max(1, int(len(words) * 1.3))

def detect_code(text: str) -> bool:
    """Detect code patterns via triple backticks or 4-space indent."""
    if re.search(r"```[\s\S]*?```", text):
        return True
    if re.search(r"^    \S", text, re.MULTILINE):
        return True
    return False

def detect_url(text: str) -> bool:
    """Detect URLs using regex."""
    url_pattern = r"https?://\S+|www\.\S+"
    return bool(re.search(url_pattern, text))

def detect_question(text: str) -> bool:
    """Detect questions via '?' or question words."""
    question_words = {
        "who", "what", "when", "where", "why", "how", "whom", "which", "is",
        "are", "was", "were", "do", "does", "did", "can", "could", "would",
        "should", "will", "shall", "have", "has", "had"
    }
    words = re.findall(r"\b\w+\b", text.lower())
    # Require ? mark OR question word at START of sentence
    has_question_mark = "?" in text
    starts_with_q = bool(words) and words[0] in question_words
    return has_question_mark or starts_with_q

def extract_stopwords(words: List[str]) -> List[str]:
    """Remove stopwords from word list."""
    return [w for w in words if w.lower() not in STOPWORDS]

def get_keywords(text: str, n: int = 5) -> List[str]:
    """Extract top n keywords by frequency (excluding stopwords)."""
    # Extract words, filter stopwords, then get frequency distribution
    words = re.findall(r"\b[a-zA-Z]{2,}\b", text.lower())
    filtered = extract_stopwords(words)
    freq = {}
    for word in filtered:
        freq[word] = freq.get(word, 0) + 1
    # Sort by frequency (desc) then alphabetically
    return [word for word, _ in sorted(freq.items(), key=lambda x: (-x[1], x[0]))[:n]]

def extract_entities(text: str) -> List[str]:
    """Extract named entities, using spacy or fallback."""
    if SPACY_AVAILABLE:
        doc = NLP(text)
        return [ent.text for ent in doc.ents if ent.text.strip()]
    else:
        # Fallback: try to extract common entity patterns (capitalized words)
        entities = []
        # Match sequences of capitalized words
        matches = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b", text)
        # Filter obvious false positives (like sentences starting with capitals)
        for match in matches:
            if len(match.split()) > 1 or match.lower() not in ["I", "A", "The"]:
                entities.append(match)
        return entities[:10]  # Limit to 10 entities

def extract_noun_phrases(text: str) -> List[str]:
    """Extract noun phrases, using spacy or fallback."""
    if SPACY_AVAILABLE:
        doc = NLP(text)
        return [chunk.text for chunk in doc.noun_chunks]
    else:
        # Fallback: simple regex for noun-like phrases
        # Matches: adjective(s) + noun(s) pattern (simplified)
        pattern = r"\b((?:[a-z]+ )*([a-z]+))\b"
        candidates = re.findall(pattern, text.lower())
        # Filter by POS-like heuristic: prefer words that end in -tion, -ness, -ity, etc.
        noun_like = [c[1] for c in candidates if len(c[1]) > 2 and any(c[1].endswith(suff) for suff in ("tion", "ness", "ity", "ment", "ship", "hood", "ness", "er", "ing"))]
        # Also try to find common noun patterns
        return list(set(noun_like))[:10]  # Deduplicate and limit

def extract_features(user_text: str, assistant_text: str) -> MessageFeatures:
    """Extract features from user and assistant messages."""
    combined_text = f"{user_text}\n{assistant_text}"
    token_count = estimate_tokens(combined_text)
    
    # Use spacy for advanced extraction if available
    if SPACY_AVAILABLE:
        doc = NLP(combined_text)
        entities = [ent.text for ent in doc.ents if ent.text.strip()]
        noun_phrases = [chunk.text for chunk in doc.noun_chunks]
    else:
        entities = extract_entities(combined_text)
        noun_phrases = extract_noun_phrases(combined_text)
    
    features = MessageFeatures(
        token_count=token_count,
        entities=entities,
        noun_phrases=noun_phrases,
        contains_code=detect_code(combined_text),
        contains_url=detect_url(combined_text),
        is_question=detect_question(user_text),
        keywords=get_keywords(combined_text)
    )
    return features
