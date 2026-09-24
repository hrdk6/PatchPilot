from calc_service.reporting import Bucket, completion_rate, summarize


def test_completion_rate():
    assert completion_rate(Bucket("alpha", completed=5, expected=10)) == 50.0


def test_summarize_includes_overall():
    report = summarize(
        [
            Bucket("alpha", completed=5, expected=10),
            Bucket("beta", completed=10, expected=10),
        ]
    )
    assert "alpha: 50.0%" in report
    assert "overall: 75.0%" in report


def test_summarize_handles_empty_bucket():
    report = summarize([Bucket("empty", completed=0, expected=0)])
    assert "empty: 0.0%" in report
