from unittest.mock import Mock, patch

import config
from refresh_eat_cookies import WANTED, _cookie_string, _cookies_pass_api
from utils import update_eat_cookie


def test_full_servicepipe_cookie_set_is_preserved():
    assert {
        "__js_p_",
        "__jhash_",
        "__jua_",
        "__hash_",
        "__lhash_",
    }.issubset(WANTED)


def test_cookie_string_keeps_full_browser_set():
    cookies = [{"name": name, "value": str(i)} for i, name in enumerate(WANTED)]
    result = _cookie_string(cookies)
    assert "__js_p_=0" in result
    assert "__jua_=2" in result
    assert "__lhash_=4" in result


def test_cookie_verification_requires_json_response():
    response = Mock(status_code=200, headers={"content-type": "application/json; charset=utf-8"})
    with patch("requests.post", return_value=response) as post:
        assert _cookies_pass_api("__hash_=ok", "Browser") is True
    assert post.call_args.kwargs["headers"]["Cookie"] == "__hash_=ok"

    response.headers = {"content-type": "text/html"}
    with patch("requests.post", return_value=response):
        assert _cookies_pass_api("__hash_=bad", "Browser") is False


def test_update_env_writes_project_env_and_syncs_process(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("EAT_COOKIE=old\nEAT_USER_AGENT=Old\n", encoding="utf-8")
    monkeypatch.setattr(update_eat_cookie, "ENV_PATH", env_path)
    monkeypatch.setenv("EAT_COOKIE", "old")
    monkeypatch.setenv("EAT_USER_AGENT", "Old")
    monkeypatch.setattr(config, "EAT_COOKIE", "old")
    monkeypatch.setattr(config, "EAT_USER_AGENT", "Old")

    update_eat_cookie.update_env("__hash_=fresh", "FreshBrowser")

    saved = env_path.read_text(encoding="utf-8")
    assert "EAT_COOKIE=__hash_=fresh" in saved
    assert "EAT_USER_AGENT=FreshBrowser" in saved
    assert config.EAT_COOKIE == "__hash_=fresh"
    assert config.EAT_USER_AGENT == "FreshBrowser"
