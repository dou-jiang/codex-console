from types import SimpleNamespace

from src.core.registration_batch_metrics import apply_task_outcome, build_domain_stats


def test_apply_task_outcome_resets_consecutive_failures_after_success():
    state = {"completed": 0, "success": 0, "failed": 0, "consecutive_failures": 0}

    for status in ["failed", "failed", "completed", "failed"]:
        apply_task_outcome(state, status)

    assert state == {
        "completed": 4,
        "success": 1,
        "failed": 3,
        "consecutive_failures": 1,
    }


def test_build_domain_stats_sorts_by_success_rate_then_volume_and_puts_missing_email_last():
    tasks = [
        SimpleNamespace(status="completed", email_address="a@yahoo.com"),
        SimpleNamespace(status="completed", email_address="b@yahoo.com"),
        SimpleNamespace(status="failed", email_address="c@yahoo.com"),
        SimpleNamespace(status="failed", email_address="d@yahoo.com"),
        SimpleNamespace(status="completed", email_address="e@gmail.com"),
        SimpleNamespace(status="failed", email_address="f@gmail.com"),
        SimpleNamespace(status="failed", email_address=None),
    ]

    stats = build_domain_stats(tasks)

    assert [row["domain"] for row in stats] == ["yahoo.com", "gmail.com", "未获取邮箱"]
    assert stats[0]["success_rate"] == stats[1]["success_rate"] == 50.0
    assert stats[0]["total"] == 4
    assert stats[1]["total"] == 2
    assert stats[2]["failure_rate"] == 100.0
