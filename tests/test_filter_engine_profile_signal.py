import filter_engine as fe
import search_profiles as sp


def _run(title: str):
    profiles = sp._default_profiles_as_objects()
    tender = {"title": title, "primary_text": "", "purchase_number": "test"}
    results = sp.score_tender(tender, profiles=profiles)
    best = sp.best_match(results)
    if best is not None:
        tender["matched_profiles"] = [r.to_dict() for r in results if r.accepted]
        tender["profile_id"] = best.profile_id
        tender["profile_score"] = best.score
    fr = fe.run_stage1_filters(tender, tender["primary_text"])
    return fr.filters[0], best


def test_strong_profile_score_bumps_f1_up():
    f1, best = _run("Разработка сайта компании с личным кабинетом на 1С-Битрикс")
    assert best is not None
    assert best.score >= best.min_score + 4
    assert any("профиль" in s for s in f1.signals)


def test_no_profile_match_leaves_f1_untouched():
    f1, best = _run("Закупка канцелярских товаров для офиса")
    assert best is None
    assert not any("профиль «" in s for s in f1.signals)


def test_soft_profile_filter_keeps_unmatched_and_penalizes_f1():
    profile = sp.Profile(
        id=1, name="IT", min_score=3,
        phrases=[sp.Phrase(id=None, kind="plus", phrase="разработк* сайт*", weight=3)],
    )
    tender = {
        "title": "Закупка канцелярских товаров",
        "primary_text": "", "purchase_number": "eat-soft",
    }
    kept, unmatched = sp.filter_and_tag(
        [tender], profiles=[profile], keep_unmatched=True,
    )

    assert kept == [tender]
    assert unmatched == 1
    assert tender["profile_filter_rejected"] is True
    baseline_tender = {k: v for k, v in tender.items() if k != "profile_filter_rejected"}
    baseline = fe.run_stage1_filters(baseline_tender, "")
    penalized = fe.run_stage1_filters(tender, "")
    assert penalized.total_score < baseline.total_score
    assert any("не прошла порог" in s for s in penalized.filters[0].signals)


def test_penalty_only_acceptance_nudges_f1_down_unless_score_still_high():
    # "поставка" минусует, но 1С-Битрикс поднимает скор выше bonus-порога —
    # тут срабатывает бонус, а не штраф (см. _apply_profile_signal: бонус приоритетнее).
    f1, best = _run("Поставка, внедрение и доработка сайта на 1С-Битрикс")
    assert best is not None
    penalty_hits = [h for h in best.hits if h[1] < 0]
    assert penalty_hits
    assert best.score >= best.min_score + 4


def test_profile_specific_price_corridor_overrides_global_config():
    # Профиль с высоким индивидуальным ценовым коридором не должен отсекать
    # тендер, который по глобальным настройкам ушёл бы в стоп-фактор Ф2.
    profile = sp.Profile(
        id=1, name="Крупные проекты", min_score=1,
        price_target_lo=1_000_000, price_target_hi=3_000_000, price_hard_max=5_000_000,
        phrases=[sp.Phrase(id=None, kind="plus", phrase="разработк* сайт*", weight=3)],
    )
    tender = {
        "title": "Разработка сайта крупного портала",
        "primary_text": "", "purchase_number": "test-price",
        "price": 2_000_250,  # выше глобального PRICE_HARD_MAX, но внутри профиля
    }
    kept, dropped = sp.filter_and_tag([tender], profiles=[profile])
    assert dropped == 0 and kept
    fr = fe.run_stage1_filters(tender, tender["primary_text"])
    f2 = fr.filters[1]
    assert f2.stop_factor is False
    assert f2.score >= 3  # попал в целевую зону профиля → бонус, не штраф


def test_no_profile_price_override_falls_back_to_global_config():
    profile = sp.Profile(
        id=1, name="Дефолтный коридор", min_score=1,
        phrases=[sp.Phrase(id=None, kind="plus", phrase="разработк* сайт*", weight=3)],
    )
    tender = {
        "title": "Разработка сайта крупного портала",
        "primary_text": "", "purchase_number": "test-price-2",
        "price": 2_000_250,  # выше глобального PRICE_HARD_MAX (1 100 000)
    }
    kept, dropped = sp.filter_and_tag([tender], profiles=[profile])
    assert dropped == 0 and kept
    fr = fe.run_stage1_filters(tender, tender["primary_text"])
    f2 = fr.filters[1]
    assert f2.stop_factor is True
