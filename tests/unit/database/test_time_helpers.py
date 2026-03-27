from src.time_utils import utc_now_naive


def test_utc_now_naive_returns_naive_datetime():
    value = utc_now_naive()

    assert value.tzinfo is None
