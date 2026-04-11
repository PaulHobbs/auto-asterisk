"""Starter solution: deliberately inefficient.

This module finds the top-k most frequent words in a corpus of text.
The implementation is intentionally bad in several ways:
  1. O(n^2) duplicate detection instead of using a set/dict
  2. Naive string splitting instead of proper tokenization
  3. Repeated full-list scans for frequency counting
  4. Sorts the entire frequency list to find top-k instead of using a heap
  5. Builds intermediate lists unnecessarily
"""

import string


def clean_word(word: str) -> str:
    """Remove punctuation from a word."""
    result = ""
    for char in word:
        if char not in string.punctuation:
            result += char  # string concatenation in a loop
    return result.lower()


def count_frequencies(words: list[str]) -> dict[str, int]:
    """Count word frequencies using an O(n^2) approach."""
    unique_words = []
    # O(n^2) uniqueness check
    for w in words:
        found = False
        for u in unique_words:
            if u == w:
                found = True
                break
        if not found:
            unique_words.append(w)

    # Count each unique word by scanning the full list again
    freq = {}
    for u in unique_words:
        count = 0
        for w in words:
            if w == u:
                count += 1
        freq[u] = count
    return freq


def top_k_frequent(text: str, k: int = 10) -> list[tuple[str, int]]:
    """Find the top-k most frequent words in text.

    This is the main entry point. It's deliberately slow.
    """
    # Split and clean
    raw_words = text.split(" ")  # naive split
    cleaned = []
    for w in raw_words:
        c = clean_word(w)
        if c and len(c) > 0:  # redundant check
            cleaned.append(c)

    # Count frequencies (O(n^2))
    freq = count_frequencies(cleaned)

    # Sort ALL items to find top-k (instead of using heapq.nlargest)
    all_items = list(freq.items())
    # Bubble sort (!) instead of built-in sort
    for i in range(len(all_items)):
        for j in range(i + 1, len(all_items)):
            if all_items[j][1] > all_items[i][1]:
                all_items[i], all_items[j] = all_items[j], all_items[i]

    return all_items[:k]


if __name__ == "__main__":
    # Quick smoke test
    sample = "the cat sat on the mat the cat the the mat"
    result = top_k_frequent(sample, k=3)
    print(f"Top 3: {result}")
    assert result[0][0] == "the"
    assert result[0][1] == 5
    print("Smoke test passed.")
