from contextlib import contextmanager
from unittest.mock import Mock, patch

import database as db
import filter_engine as fe
import search_profiles as sp


@contextmanager
def _default_filter_rules():
    # Тесты не зависят от доступности PostgreSQL и правил конкретного окружения.
    with patch.object(fe, "_RULES_CACHE", {}), patch.object(fe, "_RULES_CACHE_TS", 10**12):
        yield


def _tender(title: str, primary_text: str = "") -> dict:
    return {
        "purchase_number": "test-1",
        "title": title,
        "primary_text": primary_text,
        "price": 200_000,
        "deadline": "31.12.2026",
    }


def test_storefront_boilerplate_does_not_match_portal_profile():
    profile = sp.Profile(
        id=1,
        name="Сайты",
        min_score=3,
        phrases=[sp.Phrase(None, "plus", "портал", 3)],
    )
    tender = _tender(
        "Техническое обслуживание автомобиля",
        "Закупка малого объёма (Портал поставщиков)",
    )

    kept, dropped = sp.filter_and_tag([tender], profiles=[profile])

    assert kept == []
    assert dropped == 1


def test_document_boilerplate_cannot_rescue_unrelated_subject():
    tender = _tender(
        "Оказание услуг по техническому осмотру транспортных средств",
        "Закупка малого объёма (Портал поставщиков)",
    )
    with _default_filter_rules():
        result = fe.run_stage2_filters(tender, "Инструкция: войдите в личный кабинет")

    assert result.filters[0].score == 2
    assert result.decision == "NO-GO"


def test_custom_web_development_gets_priority_profile_score():
    with _default_filter_rules():
        result = fe.run_stage2_filters(_tender("Оказание услуг по разработке сайта"))

    assert result.filters[0].score >= 4
    assert result.decision in {"GO", "CAUTION"}
    assert any("заказная веб/ПО/API-разработка" in s for s in result.filters[0].signals)


def test_eis_broken_word_creation_still_gets_development_priority():
    with _default_filter_rules():
        result = fe.run_stage2_filters(
            _tender("Выполнение работ по создан ию информационной системы — геопортала")
        )

    assert result.filters[0].score >= 4
    assert any("заказная веб/ПО/API-разработка" in s for s in result.filters[0].signals)


def test_project_documentation_is_not_software_development():
    with _default_filter_rules():
        result = fe.run_stage2_filters(
            _tender("Разработка проектно-сметной документации на капитальный ремонт")
        )

    assert result.filters[0].score <= 2
    assert result.decision == "NO-GO"


def test_generic_1c_is_secondary_caution_but_integration_is_preferred():
    with _default_filter_rules():
        generic = fe.run_stage2_filters(
            {
                **_tender("Адаптация и модификация продукта на платформе 1С:Предприятие"),
                "profile_filter_rejected": True,
            }
        )
        integrated = fe.run_stage2_filters(_tender("Интеграция 1С с сайтом через API"))

    assert generic.filters[0].score == 3
    assert generic.decision == "CAUTION"
    assert integrated.filters[0].score >= 4
    assert not any("вторичный профиль" in s for s in integrated.filters[0].signals)


def test_bitrix_license_renewal_is_resale_not_web_development():
    with _default_filter_rules():
        result = fe.run_stage2_filters(
            _tender('Программа для ЭВМ "1С-Битрикс: Управление сайтом". Лицензия Стандарт (продление)')
        )

    assert result.filters[0].stop_factor is True
    assert result.decision == "NO-GO"


def test_detail_candidates_exclude_profile_nogo_before_network_work():
    cursor = Mock()
    cursor.fetchall.return_value = []
    connection = Mock()
    connection.cursor.return_value = cursor

    @contextmanager
    def fake_conn():
        yield connection

    with patch.object(db, "_conn", fake_conn):
        db.get_detail_candidates(limit=10, min_primary_score=24)

    sql = cursor.execute.call_args.args[0]
    assert "filter_decision IS DISTINCT FROM 'NO-GO'" in sql


def test_llm_queue_keeps_only_live_or_semantic_stop_nogo():
    cursor = Mock()
    cursor.fetchall.return_value = []
    connection = Mock()
    connection.cursor.return_value = cursor

    @contextmanager
    def fake_conn():
        yield connection

    with patch.object(db, "_conn", fake_conn):
        db.get_llm_candidates(limit=10, min_score=28)

    sql = cursor.execute.call_args.args[0]
    assert "t.filter_decision IS DISTINCT FROM 'NO-GO'" in sql
    assert "fs.filter_number IN (4, 5, 6, 7)" in sql
    assert "fs.stop_factor = TRUE" in sql
