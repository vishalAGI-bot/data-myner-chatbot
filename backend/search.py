"""Lookup over sub_category names: fuzzy by default, nomic-embed-text optionally.

rapidfuzz is the default because category names are 2-4 words of plain English:
it scores them well, runs in microseconds and loads no model at all. Embeddings
win when users describe products in words that share no characters with your
labels ("thing my baby sleeps in" -> Baby Sleepwear).

Set USE_EMBEDDINGS=1 to switch to nomic-embed-text through Ollama. Vectors are
cached to disk, so the one-time embedding pass only happens when the taxonomy
or the model changes.
"""

import hashlib
import re

from rapidfuzz import fuzz, process

import config
import llm


# Question scaffolding that carries no product meaning. Dropped from the QUERY
# only, never from the labels.
STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "do", "does", "did", "of", "in",
    "on", "for", "to", "at", "by", "with", "and", "or", "it", "this", "that",
    "what", "which", "where", "who", "how", "can", "you", "i", "me", "my", "we",
    "belong", "belongs", "go", "goes", "put", "fall", "falls", "come", "comes",
    "category", "categories", "sub", "subcategory", "under", "find", "show",
    "tell", "about", "know", "want", "please", "type", "kind", "item", "product",
}


def normalise_label(text: str) -> str:
    """Labels are canonical: only lowercase and strip punctuation.

    Synonyms must NEVER be applied here. Doing so silently merges distinct
    labels - "Baby & Infant" collapses to "baby baby" under infant->baby, and
    then scores 100 against any query containing "baby".
    """
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


# Words that describe the SHAPE of a question rather than its subject. Stripped
# only when deciding whether a question names a specific value - never from the
# lookup path, where "master" could legitimately be part of a label.
META_WORDS = {
    "master", "masters", "category", "categories", "subcategory", "subcategories",
    "data", "row", "rows", "column", "table", "value", "values", "name", "names",
    "many", "much", "count", "total", "number", "list", "all", "any", "each",
    "every", "exist", "exists", "there", "here", "most", "least", "top", "few",
    "biggest", "smallest", "largest", "has", "have", "give", "need", "there's",
    "search", "searches", "searched", "query", "queries", "journey", "journeys",
    "session", "sessions", "app", "apps", "website", "websites", "user", "users",
    "event", "events", "distinct", "unique", "different", "step", "steps",
    "product", "products", "brand", "brands", "sku", "skus", "item", "items",
}


def content_terms(text: str) -> list[str]:
    """The words in a question that actually name something in the data.

    "how many categories are there?" -> []  (asks about shape, names nothing)
    "how many dairy in sub category" -> ["dairy"]

    Returning [] is the important case: a question with no subject must not be
    fuzzy-matched, or the raw sentence matches junk. That is how "are there"
    once matched "Gifts, Decor & All Other" and filtered a global count down
    to 7 rows.
    """
    out = []
    for word in re.sub(r"[^\w\s]", " ", text.lower()).split():
        if word in STOPWORDS or word in META_WORDS or len(word) < 3:
            continue
        mapped = config.SYNONYMS.get(word)
        if mapped is None and word.endswith("s"):
            mapped = config.SYNONYMS.get(word[:-1])
        out.append(mapped or word)
    return list(dict.fromkeys(out))


def normalise_query(text: str) -> str:
    """Queries are messy: drop question words, map user vocabulary onto
    taxonomy vocabulary, and de-duplicate the result."""
    words, seen = [], set()
    for word in re.sub(r"[^\w\s]", " ", text.lower()).split():
        if word in STOPWORDS:
            continue
        mapped = config.SYNONYMS.get(word)
        if mapped is None and word.endswith("s"):
            mapped = config.SYNONYMS.get(word[:-1])
        mapped = mapped or word
        if mapped not in seen:          # "baby infant" -> "baby", not "baby baby"
            seen.add(mapped)
            words.append(mapped)
    return " ".join(words) or normalise_label(text)


# kept for backwards compatibility
normalise = normalise_query


class TaxonomySearch:
    def __init__(self, rows):
        # rows: sqlite3.Row with sub_category, category, master_category
        self.rows = [dict(r) for r in rows]
        self.labels = [r["sub_category"] for r in self.rows]
        self.keys = [normalise_label(lbl) for lbl in self.labels]
        # A second, tiny index over EVERY value at EVERY level. Used to tell
        # the SQL model which real values resemble the user's wording, so it
        # stops writing `= 'Dairy'` when the label is 'Dairy & Eggs'.
        self.values = []
        for level in ("sub_category", "category", "master_category"):
            for value in dict.fromkeys(r[level] for r in self.rows if r.get(level)):
                self.values.append((value, level))
        self.value_keys = [normalise_label(v) for v, _ in self.values]

        self._vectors = None
        if config.USE_EMBEDDINGS:
            self._init_embeddings()

    def search_values(self, query: str, k: int = 6, threshold: int = 70) -> list[dict]:
        """Which real values in the data does this question actually name?

        Returns [] when the question names nothing specific. That guard is the
        whole point: a hint is only useful when there is a term to resolve, and
        a hint on a generic question makes the model invent a WHERE clause.
        """
        terms = content_terms(query)
        if not terms:
            return []

        joined = " ".join(terms)
        shortlist = process.extract(
            joined, self.value_keys,
            scorer=fuzz.partial_token_set_ratio, limit=40,
        )

        out = []
        for _, _, idx in shortlist:
            key = self.value_keys[idx]
            # best single term match, blended with how well the whole phrase fits
            per_term = max(fuzz.partial_ratio(t, key) for t in terms)
            whole = fuzz.token_set_ratio(joined, key)
            score = 0.5 * per_term + 0.5 * whole
            if score >= threshold:
                value, level = self.values[idx]
                out.append({"value": value, "level": level,
                            "score": round(float(score), 1)})
        out.sort(key=lambda d: -d["score"])
        return out[:k]

    def search(self, query: str, k: int = None, threshold: int = None) -> list[dict]:
        k = k or config.FUZZY_TOP_K
        threshold = threshold if threshold is not None else config.FUZZY_THRESHOLD

        if self._vectors is not None:
            return self._search_embeddings(query, k, threshold)
        return self._search_fuzzy(query, k, threshold)

    def _search_fuzzy(self, query, k, threshold):
        """Two stages.

        token_set_ratio is fast and finds candidates, but it returns 100
        whenever the query's words are a SUBSET of the label's - so "baby
        bottoms" ties with "baby boys bottoms" and the winner is arbitrary.
        token_sort_ratio does not have that blind spot but is stricter about
        word order, so the top candidates are re-scored with a blend of both.
        Only ~4k candidates get the slower pass, so this stays fast on large
        taxonomies.
        """
        key = normalise_query(query)
        candidates = process.extract(
            key, self.keys, scorer=fuzz.token_set_ratio, limit=max(k * 4, 20)
        )

        rescored = []
        for _, coarse, idx in candidates:
            fine = fuzz.token_sort_ratio(key, self.keys[idx])
            rescored.append((0.6 * coarse + 0.4 * fine, idx))
        rescored.sort(key=lambda pair: -pair[0])

        out = []
        for score, idx in rescored[:k]:
            if score >= threshold:
                out.append({**self.rows[idx], "score": round(float(score), 1)})
        return out

    def _search_embeddings(self, query, k, threshold):
        import numpy as np

        # nomic-embed-text is trained with task prefixes; using them lifts
        # retrieval quality noticeably over raw text.
        vec = llm.embed([f"search_query: {query}"])[0]
        sims = self._vectors @ vec
        order = np.argsort(-sims)[:k]
        out = []
        for idx in order:
            score = float(sims[idx]) * 100
            if score >= threshold:
                out.append({**self.rows[int(idx)], "score": round(score, 1)})
        return out

    def match_query_to_taxonomy(self, query: str, threshold: int = None):
        """Find a taxonomy label contained within a longer search query."""
        threshold = threshold if threshold is not None else config.LINK_THRESHOLD
        key = normalise_query(query)
        if not key:
            return None

        best = None
        for index, label_key in enumerate(self.keys):
            score = fuzz.partial_token_set_ratio(label_key, key)
            if score < threshold or (best and score <= best[0]):
                continue
            words = [word for word in label_key.split() if len(word) >= 4]
            if not words or max(fuzz.partial_ratio(word, key) for word in words) < 85:
                continue
            best = (score, index)

        if not best:
            return None
        score, index = best
        return {**self.rows[index], "score": round(float(score), 1)}

    @staticmethod
    def to_text(hits: list[dict]) -> str:
        if not hits:
            return "(no close match found)"
        lines = ["sub_category | category | master_category | match_score"]
        for h in hits:
            lines.append(
                f"{h['sub_category']} | {h['category']} | {h['master_category']} | {h['score']}"
            )
        return "\n".join(lines)
