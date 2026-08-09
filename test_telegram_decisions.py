from unittest.mock import Mock, patch

import telegram_decisions as td


def _ok_response():
    response = Mock()
    response.status_code = 200
    response.text = "ok"
    return response


def test_clear_deletes_private_chat_in_batches():
    message = {
        "text": "/clear",
        "message_id": 205,
        "chat": {"id": 42, "type": "private"},
    }

    with patch.object(td.config, "TELEGRAM_CHAT_ID", "42"), patch.object(
        td.requests, "post", return_value=_ok_response()
    ) as post:
        td.handle_message("token", message)

    calls = [call for call in post.call_args_list if call.args[0].endswith("/deleteMessages")]
    assert len(calls) == 3
    deleted_ids = [message_id for call in calls for message_id in call.kwargs["json"]["message_ids"]]
    assert sorted(deleted_ids) == list(range(1, 206))


def test_clear_ignores_another_chat():
    message = {
        "text": "/clear",
        "message_id": 10,
        "chat": {"id": 99, "type": "private"},
    }

    with patch.object(td.config, "TELEGRAM_CHAT_ID", "42"), patch.object(td.requests, "post") as post:
        td.handle_message("token", message)

    post.assert_not_called()


def test_clear_does_not_delete_group_history():
    message = {
        "text": "/clear@tender_bot",
        "message_id": 10,
        "chat": {"id": -42, "type": "supergroup"},
    }

    with patch.object(td.config, "TELEGRAM_CHAT_ID", "-42"), patch.object(
        td.requests, "post", return_value=_ok_response()
    ) as post:
        td.handle_message("token", message)

    assert post.call_count == 1
    assert post.call_args.args[0].endswith("/sendMessage")
