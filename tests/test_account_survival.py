from types import SimpleNamespace

from src.core.account_survival import classify_survival_result, probe_account_survival


def test_classify_survival_result_marks_invalid_refresh_as_dead():
    assert classify_survival_result(signal_type="refresh_invalid", status_code=401) == "dead"


def test_classify_survival_result_marks_successful_probe_as_healthy():
    assert classify_survival_result(signal_type="refresh_ok", status_code=200) == "healthy"


def test_probe_account_survival_marks_missing_tokens_as_warning():
    account = SimpleNamespace(
        id=1,
        status="active",
        access_token=None,
        refresh_token=None,
        session_token=None,
    )

    result = probe_account_survival(account)

    assert result["result_level"] == "warning"
    assert result["signal_type"] == "token_missing"
