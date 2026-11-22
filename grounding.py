"""Optional dictionary hints. No automatic corpus download or network access."""

from typing import Protocol


class Grounding(Protocol):
    def lookup(self, word: str) -> list[str]: ...


class WordNetGrounding:
    def lookup(self, word: str) -> list[str]:
        try:
            from nltk.corpus import wordnet

            return [synset.definition() for synset in wordnet.synsets(word.replace(" ", "_"))[:3]]
        except (ImportError, LookupError) as exc:
            raise RuntimeError(
                "WordNet enabled but unavailable; install the wordnet extra and corpus, "
                "or set VOCAB_WORDNET=false"
            ) from exc
