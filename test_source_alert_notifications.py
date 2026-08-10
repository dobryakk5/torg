from unittest.mock import patch

import main
import notifier


def test_compact_source_errors_collapses_phrases_per_source():
    errors = [
        "Ошибка ЕАТ-поиска по 'one': антибот",
        "Ошибка ЕАТ-поиска по 'two': антибот",
        "Ошибка ЭМ МО-поиска по 'one': timeout",
    ]

    assert notifier.compact_source_errors(errors) == [
        ("eat", "Ошибка доступа ЕАТ"),
        ("mosreg", "Ошибка доступа ЭМ МО"),
    ]


def test_daily_source_alert_is_sent_only_once(monkeypatch):
    saved = {}
    monkeypatch.setattr(main.db, "get_setting", lambda key, default=None: saved.get(key, default))
    monkeypatch.setattr(main.db, "set_setting", lambda key, value, description="": saved.__setitem__(key, value))
    monkeypatch.setattr(main.db, "had_stage1_error_today_before", lambda needle, before: False)

    with patch.object(main, "send_source_error_alert", return_value=True) as send:
        main._send_source_alerts_once_daily(["Ошибка ЕАТ-поиска: антибот"], "2026-08-10T09:00:00")
        main._send_source_alerts_once_daily(["Ошибка ЕАТ-поиска: антибот"], "2026-08-10T09:15:00")

    send.assert_called_once()
    assert send.call_args.args[2] == ["Ошибка доступа ЕАТ"]


def test_existing_error_in_today_history_suppresses_first_new_style_alert(monkeypatch):
    saved = {}
    monkeypatch.setattr(main.db, "get_setting", lambda key, default=None: saved.get(key, default))
    monkeypatch.setattr(main.db, "set_setting", lambda key, value, description="": saved.__setitem__(key, value))
    monkeypatch.setattr(main.db, "had_stage1_error_today_before", lambda needle, before: needle == "ЕАТ")

    with patch.object(main, "send_source_error_alert", return_value=True) as send:
        main._send_source_alerts_once_daily(
            ["Ошибка ЕАТ-поиска: антибот"],
            "2026-08-10T09:15:00",
        )

    send.assert_not_called()
    assert saved[main.SOURCE_ALERT_SETTING_PREFIX + "EAT"]


def test_eat_queries_are_combined_to_reduce_antibot_requests():
    assert main._eat_combined_queries(["сайт*", "программн*", "сайт*"]) == [
        "сайт* ||программн*"
    ]
