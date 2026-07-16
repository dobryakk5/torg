from unittest.mock import Mock, patch

import llm_provider


def _response(status_code: int, body: dict | None = None) -> Mock:
    response = Mock()
    response.status_code = status_code
    response.headers = {}
    response.text = "Access denied by security policy"
    response.json.return_value = body or {}
    return response


def test_openrouter_retries_temporary_403_before_fallback():
    denied = _response(403)
    success = _response(
        200,
        {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]},
    )

    with (
        patch.object(llm_provider, "_openrouter_post", side_effect=[denied, success]) as post,
        patch.object(llm_provider, "_bump_daily_count"),
        patch.object(llm_provider.time, "sleep") as sleep,
    ):
        result = llm_provider._complete_openrouter(
            "system", "user", "test/model", 64, 0.0, False,
        )

    assert result == "OK"
    assert post.call_count == 2
    sleep.assert_called_once_with(5)
