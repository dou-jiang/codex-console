from packages.registration_core.client import OpenAIRegistrationClient


def test_client_can_be_constructed_without_web_imports():
    client = OpenAIRegistrationClient(proxy_url=None)

    assert client is not None
    assert client.http_client is not None
    assert client.oauth_manager is not None
