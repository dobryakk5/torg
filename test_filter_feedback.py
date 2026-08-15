import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock, patch

import database as db
import pytest
import web_app
from database import _feedback_topics


def test_feedback_topics_groups_rejected_titles_and_keeps_examples():
    rows = [
        {"purchase_number": "1", "title": "Разработка проектной документации"},
        {"purchase_number": "2", "title": "Переустройство помещения"},
        {"purchase_number": "3", "title": "Техническое обслуживание автомобиля"},
        {"purchase_number": "4", "title": "Разработка информационной системы"},
    ]

    topics = _feedback_topics(rows)

    assert topics[0]["name"] == "Строительство и проектирование"
    assert topics[0]["count"] == 2
    assert [item["purchase_number"] for item in topics[0]["examples"]] == ["1", "2"]
    assert topics[1]["name"] == "Автомобили и транспорт"
    assert topics[1]["count"] == 1
    assert sum(topic["count"] for topic in topics) == 3


def test_feedback_topics_limits_examples_to_three():
    rows = [
        {"purchase_number": str(i), "title": f"Строительство объекта {i}"}
        for i in range(5)
    ]

    topics = _feedback_topics(rows)

    assert topics[0]["count"] == 5
    assert len(topics[0]["examples"]) == 3


def _fake_db_context(profile, existing):
    cursor = Mock()
    cursor.fetchone.return_value = profile
    cursor.fetchall.return_value = existing
    connection = Mock()
    connection.cursor.return_value = cursor

    @contextmanager
    def fake_conn():
        yield connection

    return fake_conn, cursor


def test_add_default_hard_minus_inserts_once_into_default_profile():
    fake_conn, cursor = _fake_db_context({"id": 7, "name": "Основной"}, [])

    with patch.object(db, "_conn", fake_conn):
        result = db.search_default_hard_minus_add("  проектная   документация ")

    assert result == {
        "added": True,
        "action": "added",
        "profile_id": 7,
        "profile_name": "Основной",
        "phrase": "проектная документация",
    }
    insert = next(call for call in cursor.execute.call_args_list if "INSERT INTO search_phrases" in call.args[0])
    assert insert.args[1] == (7, "проектная документация")


def test_add_default_hard_minus_is_idempotent():
    existing = [{"id": 11, "kind": "minus_hard", "phrase": "строительство"}]
    fake_conn, cursor = _fake_db_context({"id": 7, "name": "Основной"}, existing)

    with patch.object(db, "_conn", fake_conn):
        result = db.search_default_hard_minus_add("Строительство")

    assert result["added"] is False
    assert result["action"] == "already_exists"
    assert not any("INSERT INTO search_phrases" in call.args[0] for call in cursor.execute.call_args_list)


def test_add_default_hard_minus_promotes_soft_minus():
    existing = [{"id": 12, "kind": "minus_soft", "phrase": "строительство"}]
    fake_conn, cursor = _fake_db_context({"id": 7, "name": "Основной"}, existing)

    with patch.object(db, "_conn", fake_conn):
        result = db.search_default_hard_minus_add("строительство")

    assert result["action"] == "promoted"
    update = next(call for call in cursor.execute.call_args_list if "UPDATE search_phrases" in call.args[0])
    assert update.args[1] == (12,)


def test_add_default_hard_minus_reactivates_disabled_hard_minus():
    existing = [{"id": 14, "kind": "minus_hard", "phrase": "строительство", "enabled": False}]
    fake_conn, cursor = _fake_db_context({"id": 7, "name": "Основной"}, existing)

    with patch.object(db, "_conn", fake_conn):
        result = db.search_default_hard_minus_add("строительство")

    assert result["action"] == "reactivated"
    update = next(call for call in cursor.execute.call_args_list if "UPDATE search_phrases" in call.args[0])
    assert update.args[1] == (14,)


def test_add_default_hard_minus_rejects_plus_phrase_conflict():
    existing = [{"id": 13, "kind": "plus", "phrase": "строительство"}]
    fake_conn, _ = _fake_db_context({"id": 7, "name": "Основной"}, existing)

    with patch.object(db, "_conn", fake_conn), pytest.raises(ValueError, match="плюс-фраза"):
        db.search_default_hard_minus_add("строительство")


def test_analytics_endpoint_invalidates_profile_cache():
    request = Mock()
    request.json = AsyncMock(return_value={"phrase": "строительство"})
    expected = {
        "added": True,
        "action": "added",
        "profile_id": 7,
        "profile_name": "Основной",
        "phrase": "строительство",
    }

    with patch.object(web_app.db, "search_default_hard_minus_add", return_value=expected), patch(
        "search_profiles.invalidate_cache"
    ) as invalidate:
        result = asyncio.run(web_app.api_analytics_add_default_hard_minus(request))

    assert result == {"ok": True, **expected}
    invalidate.assert_called_once_with()
