from src.core.account_survival_dispatcher import AccountSurvivalDispatcher


class FakeRepo:
    def __init__(self):
        self.claim_calls = 0
        self.recorded = []

    def claim_due_checks(self, *, limit: int = 50):
        self.claim_calls += 1
        return [
            {"check_id": 1, "account_id": 101},
            {"check_id": 2, "account_id": 102},
        ]

    def record_result(self, claimed_check, result):
        self.recorded.append((claimed_check, result))


def test_dispatcher_claims_due_checks_and_records_completed_results():
    repo = FakeRepo()
    dispatcher = AccountSurvivalDispatcher(
        repo=repo,
        probe_func=lambda item: {"result_level": "healthy", "signal_type": "refresh_ok"},
    )

    processed = dispatcher.dispatch_due_checks_once()

    assert processed == 2
    assert repo.claim_calls == 1
    assert len(repo.recorded) == 2
    assert all(result["result_level"] == "healthy" for _, result in repo.recorded)
