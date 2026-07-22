from wereadit.models import ReadResult


def test_read_result_has_warmup_fields() -> None:
    r = ReadResult(
        completed_count=120,
        total_minutes=60.0,
        warmup_done=True,
        warmup_attempts=2,
    )
    assert r.warmup_done is True
    assert r.warmup_attempts == 2
    assert "预热" in r.summary()
