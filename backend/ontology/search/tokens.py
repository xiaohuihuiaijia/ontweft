"""Versioned, deterministic Chinese search tokenization; no downloaded models."""

import hashlib
import importlib.metadata
import re
import tempfile
import threading
import unicodedata
from pathlib import Path

TOKENIZER_VERSION = "jieba-0.42.1-dict7197c321-hmm0-nfkc-casefold-search-v1"
DICT_SHA256 = "7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8"
_lock = threading.Lock()
_tokenizer = None
_cache = None


class SearchBudgetError(ValueError):
    pass


def tokenizer():
    global _tokenizer, _cache
    with _lock:
        if _tokenizer is None:
            import jieba

            if importlib.metadata.version("jieba") != "0.42.1":
                raise RuntimeError("Search tokenizer version differs from the reviewed version")
            dictionary = Path(jieba.__file__).parent / "dict.txt"
            if hashlib.sha256(dictionary.read_bytes()).hexdigest() != DICT_SHA256:
                raise RuntimeError("Search dictionary differs from the reviewed artifact")
            # Never load a shared, externally writable jieba cache. The package
            # imports finalseg, but this application never executes HMM/Paddle.
            _cache = tempfile.TemporaryDirectory(prefix="ontology-tokenizer-")
            instance = jieba.Tokenizer(str(dictionary))
            instance.tmp_dir = _cache.name
            instance.initialize()
            _tokenizer = instance
    return _tokenizer


def tokens(value: str, *, maximum=100000) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    result = []
    for word in tokenizer().cut_for_search(normalized, HMM=False):
        # Each emitted lexeme is safe for the PG simple text parser. Query
        # strings never become SQL or tsquery operators.
        for term in re.findall(r"[^\W_]+", word, flags=re.UNICODE):
            if len(term.encode("utf-8")) > 512:
                raise SearchBudgetError("单个检索词过长，请拆分连续文本")
            result.append(term)
            if len(result) > maximum:
                raise SearchBudgetError("全文词项数量超过单对象处理预算")
    return result


def query_tokens(value: str) -> list[str]:
    result = list(dict.fromkeys(tokens(value, maximum=200)))
    if not result or len(result) > 12:
        raise SearchBudgetError("请输入可检索的词语，分词后最多 12 个不同词项")
    return result
