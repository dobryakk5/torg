from sources.eat import search_eat
from sources.mosreg import _extract_items, _map_item


def test_mosreg_extracts_current_invdata_wrapper_and_initial_price():
    raw = {
        "totalpages": 1,
        "currpage": 1,
        "totalrecords": 1,
        "invdata": [{
            "TradeState": 15,
            "TradeName": "Оказание услуг по сайту",
            "CustomerFullName": "Заказчик",
            "Id": 3722711,
            "InitialPrice": 52200.0,
            "FillingApplicationEndDate": "2026-08-10T07:00:00Z",
            "PublicationDate": "2026-08-06T07:00:01.453Z",
        }],
    }

    items = _extract_items(raw)
    assert len(items) == 1
    card = _map_item(items[0])
    assert card is not None
    assert card["purchase_number"] == "MOSREG-3722711"
    assert card["price"] == 52200.0


def test_eat_connection_failure_is_not_reported_as_empty_list(monkeypatch):
    monkeypatch.setattr("sources.eat._post", lambda body: None)

    try:
        search_eat("сайт", pages=1)
    except RuntimeError as exc:
        assert "антиботом/капчей" in str(exc)
    else:
        raise AssertionError("Сбой ЕАТ не должен превращаться в пустой список")
