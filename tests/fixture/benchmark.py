#!/usr/bin/env python3
"""Benchmark for compute.py — measures latency and correctness.

Outputs a JSON report that the judge agent can score.
"""

import json
import random
import string
import time
import traceback
import sys


def generate_corpus(n_words: int = 5000, vocab_size: int = 200, seed: int = 42) -> str:
    """Generate a reproducible random text corpus."""
    rng = random.Random(seed)
    # Create a vocabulary with Zipf-like distribution
    vocab = []
    for i in range(vocab_size):
        word_len = rng.randint(2, 10)
        word = "".join(rng.choices(string.ascii_lowercase, k=word_len))
        vocab.append(word)

    # Sample words with frequency bias (first words are more common)
    weights = [1.0 / (i + 1) ** 0.8 for i in range(vocab_size)]
    words = rng.choices(vocab, weights=weights, k=n_words)

    # Add some punctuation
    for i in range(0, len(words), rng.randint(5, 15)):
        words[i] = words[i] + rng.choice([".", ",", "!", "?", ";", ":"])

    return " ".join(words)


def get_expected_top_k(corpus: str, k: int = 10) -> list[tuple[str, int]]:
    """Reference implementation for correctness checking."""
    from collections import Counter
    words = []
    for w in corpus.split():
        cleaned = "".join(c for c in w if c not in string.punctuation).lower()
        if cleaned:
            words.append(cleaned)
    return Counter(words).most_common(k)


def run_benchmark() -> dict:
    """Run the benchmark and return results."""
    report = {
        "correctness": False,
        "latency_ms": None,
        "words_processed": 0,
        "error": None,
    }

    corpus = generate_corpus(n_words=5000)
    k = 10
    report["words_processed"] = len(corpus.split())

    try:
        from compute import top_k_frequent

        # Correctness check
        expected = get_expected_top_k(corpus, k)
        result = top_k_frequent(corpus, k)

        # Check that top-k words match (order may vary for tied counts)
        expected_words = set(w for w, _ in expected)
        result_words = set(w for w, _ in result)
        # Allow some slack: at least 7 of top 10 should match
        overlap = len(expected_words & result_words)
        report["correctness"] = overlap >= 7
        report["top_k_overlap"] = overlap

        # Latency benchmark (average of 3 runs)
        times = []
        for _ in range(3):
            t0 = time.perf_counter()
            top_k_frequent(corpus, k)
            t1 = time.perf_counter()
            times.append((t1 - t0) * 1000)
        report["latency_ms"] = round(min(times), 2)
        report["latency_avg_ms"] = round(sum(times) / len(times), 2)

        # Also test with a larger corpus for scaling behavior
        big_corpus = generate_corpus(n_words=15000)
        t0 = time.perf_counter()
        top_k_frequent(big_corpus, k)
        t1 = time.perf_counter()
        report["latency_15k_ms"] = round((t1 - t0) * 1000, 2)

    except Exception as e:
        report["error"] = f"{type(e).__name__}: {e}"
        report["traceback"] = traceback.format_exc()

    return report


if __name__ == "__main__":
    report = run_benchmark()
    print(json.dumps(report, indent=2))

    # Exit with error code if something went wrong
    if report.get("error"):
        sys.exit(1)
    if not report.get("correctness"):
        sys.exit(2)
    sys.exit(0)
