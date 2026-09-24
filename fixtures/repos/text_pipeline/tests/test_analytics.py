from text_pipeline.analytics import top_words, vocabulary_size, word_counts

SAMPLE = "Ship the code. Ship the tests! Ship, then measure."


def test_word_counts_merge_punctuated_variants():
    counts = word_counts(SAMPLE)
    assert counts["ship"] == 3


def test_top_words():
    assert top_words(SAMPLE, limit=2) == [("ship", 3), ("code", 1)]


def test_vocabulary_size_ignores_punctuation():
    assert vocabulary_size(SAMPLE) == 5


def test_stopwords_are_dropped():
    assert "the" not in word_counts(SAMPLE)
