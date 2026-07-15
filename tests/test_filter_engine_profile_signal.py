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


def test_penalty_only_acceptance_nudges_f1_down_unless_score_still_high():
    # "поставка" минусует, но 1С-Битрикс поднимает скор выше bonus-порога —
    # тут срабатывает бонус, а не штраф (см. _apply_profile_signal: бонус приоритетнее).
    f1, best = _run("Поставка, внедрение и доработка сайта на 1С-Битрикс")
    assert best is not None
    penalty_hits = [h for h in best.hits if h[1] < 0]
    assert penalty_hits
    assert best.score >= best.min_score + 4
