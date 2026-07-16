import search_profiles as sp


def _profiles():
    return sp._default_profiles_as_objects()


def _accept(title: str, profile_name: str, primary_text: str = ""):
    profiles = _profiles()
    results = sp.score_tender({"title": title, "primary_text": primary_text}, profiles=profiles)
    return next(r for r in results if r.profile_name == profile_name)


def test_default_profiles_include_expected_names_and_defaults():
    profiles = _profiles()
    names = [p.name for p in profiles]
    assert "Сайты и веб-разработка" in names
    assert "Разработка ПО и информационных систем" in names
    assert "Интеграции и автоматизация" in names
    # Сопровождение, маркетинг и импорт выключены по умолчанию — не участвуют в поиске.
    assert "Сопровождение и техподдержка" not in names
    assert "Маркетинг: SEO/SMM/контент" not in names
    assert "Импорт: текущий список" not in names

    default_profile = next(p for p in profiles if p.is_default)
    assert default_profile.name == "Сайты и веб-разработка"


def test_educational_program_is_rejected_by_software_profile():
    r = _accept("Разработка образовательной программы для дошкольников",
                "Разработка ПО и информационных систем")
    assert r.accepted is False
    assert r.hard_hit is not None


def test_qualified_prikladnaya_programma_is_accepted():
    r = _accept("Разработка прикладной программы учёта материалов",
                "Разработка ПО и информационных систем")
    assert r.accepted is True


def test_modular_building_does_not_match_software_module_phrase():
    r = _accept("Поставка модульных зданий, модуль строительный бытовой",
                "Разработка ПО и информационных систем")
    assert r.accepted is False
    assert r.hits == []


def test_supply_implementation_and_refinement_of_is_passes():
    # В профиле "Разработка ПО и ИС" минусуется только "поставка оборудования"
    # (узкое сочетание), голое "поставка" не штрафует — тендер должен пройти.
    r = _accept(
        "Поставка, внедрение и доработка информационной системы учёта контингента",
        "Разработка ПО и информационных систем",
    )
    assert r.accepted is True


def test_bare_postavka_is_soft_penalty_not_hard_reject_in_sites_profile():
    # В профиле "Сайты" голое "поставка" — мягкий минус, не жёсткое исключение:
    # при достаточном плюс-сигнале тендер всё равно проходит порог.
    r = _accept(
        "Поставка, внедрение и доработка сайта на 1С-Битрикс",
        "Сайты и веб-разработка",
    )
    assert r.hard_hit is None
    penalty_hits = [h for h in r.hits if h[1] < 0]
    assert penalty_hits, "ожидался минус-штраф за 'поставка'"
    assert r.accepted is True


def test_sensor_integration_is_hard_rejected():
    r = _accept("Оказание услуг по сенсорной интеграции для детей с ОВЗ",
                "Интеграции и автоматизация")
    assert r.accepted is False
    assert r.hard_hit == "сенсорн* интеграц*"


def test_obmen_dannymi_1c_does_not_false_positive_on_teploobmennik():
    # Регрессия: старый substring-матчинг на "обмен" ловил "теплообменник".
    # Токенный матчинг матчит только целые слова, начинающиеся с "обмен".
    r = _accept("Обмен данными 1С и поставка теплообменника для котельной",
                "Интеграции и автоматизация")
    assert r.accepted is True
    assert any(h[0] == "обмен* данн* 1С" for h in r.hits)

    tokens = sp.tokenize("поставка теплообменника для котельной")
    assert sp.phrase_hits(tokens, ["обмен*"], window=1) is False


def test_site_development_matches_default_profile():
    r = _accept("Разработка сайта компании с личным кабинетом",
                "Сайты и веб-разработка")
    assert r.accepted is True
    assert any("сайт" in h[0] for h in r.hits)


def test_bitrix_hyphen_word_matches_bare_alt_phrase():
    r = _accept("Техническая поддержка 1С-Битрикс интернет-магазина",
                "Сайты и веб-разработка")
    assert r.accepted is True
    hit_phrases = [h[0] for h in r.hits]
    assert "1С-Битрикс" in hit_phrases or "битрикс|bitrix" in hit_phrases


def test_best_match_picks_highest_scoring_accepted_profile():
    profiles = _profiles()
    tender = {
        "title": "Разработка сайта и интеграция с 1С, разработка информационной системы учёта",
        "primary_text": "",
    }
    results = sp.score_tender(tender, profiles=profiles)
    best = sp.best_match(results)
    assert best is not None
    assert best.accepted is True


def test_license_resale_is_blocked_but_real_development_survives():
    """Регрессия по реальной выдаче ЕИС (16.07.2026).

    ЕИС не пишет «приобретение лицензий» — пишет «предоставление/передача/продление»,
    поэтому старые фразы (приобретен* лиценз*)~2 ловили 0 из 32 реальных лотов.
    При этом в договорах на РАЗРАБОТКУ передача неисключительных прав на результат
    работ — норма, и она же попадает в primary_text; глушить её нельзя.
    """
    resale = [
        "Оказание услуг по предоставлению неисключительных прав на дополнительные модули ГИС",
        "Услуги по предоставлению лицензий на право использовать компьютерное программное обеспечение",
        "Продление лицензии на «Межсетевой экран с системой обнаружения вторжений»",
        "Оказание услуг по передаче неисключительных прав на расширенный функционал «Байкал»",
        'Лицензия на программное обеспечение для управления сайтом ("1С-Битрикс: Управление сайтом")',
    ]
    for title in resale:
        profiles = _profiles()
        results = sp.score_tender({"title": title, "primary_text": ""}, profiles=profiles)
        assert sp.best_match(results) is None, f"перекуп не заглушён: {title}"

    development = [
        "Оказание услуг по модернизации государственной информационной системы в сфере здравоохранения",
        "Оказание услуг по развитию государственной информационной системы в сфере здравоохранения",
    ]
    for title in development:
        profiles = _profiles()
        results = sp.score_tender({"title": title, "primary_text": ""}, profiles=profiles)
        assert sp.best_match(results) is not None, f"настоящая разработка заглушена: {title}"


def test_dev_contract_mentioning_rights_transfer_is_not_blocked():
    # Договорная болтовня о передаче прав в тексте страницы не должна убивать разработку.
    profiles = _profiles()
    tender = {
        "title": "Оказание услуг по модернизации информационной системы",
        "primary_text": "Исполнитель передаёт Заказчику неисключительные права на результаты работ",
    }
    assert sp.best_match(sp.score_tender(tender, profiles=profiles)) is not None


def test_parse_phrase_window_syntax():
    words, window = sp.parse_phrase("(капитальн* ремонт*)~1")
    assert words == ["капитальн*", "ремонт*"]
    assert window == 1

    words, window = sp.parse_phrase("разработк* сайт*")
    assert words == ["разработк*", "сайт*"]
    assert window == 1  # окно по умолчанию


def test_eis_queries_skip_manual_syntax_without_query_text():
    profile = sp.Profile(
        id=None, name="test", phrases=[
            sp.Phrase(id=None, kind="plus", phrase="разработк* сайт*", weight=3),
            sp.Phrase(id=None, kind="plus", phrase="битрикс|bitrix", weight=3),  # без query_text — пропускается
            sp.Phrase(id=None, kind="plus", phrase="(капитальн* ремонт*)~1", weight=3, query_text="капитальный ремонт"),
            sp.Phrase(id=None, kind="plus", phrase="доработк*", weight=2, local_only=True),  # не идёт в ЕИС
        ],
    )
    queries = sp.eis_queries(profile)
    # Без ручного query_text маска "*" просто срезается (слабый авто-откат) —
    # для реальных сидов query_text задаётся вручную (см. DEFAULT_PROFILES).
    assert "разработк сайт" in queries
    assert "капитальный ремонт" in queries
    assert "доработк" not in queries
    assert len(queries) == 2
