from sources.mos_supplier import (
    PAGE_SIZE,
    PURCHASE_TYPE_AUCTION,
    STATE_AUCTION_ACTIVE,
    _build_filter,
    _build_query_dto,
)


def test_query_dto_matches_portal_frontend_shape():
    flt = _build_filter("сайт", None, None, [])
    dto = _build_query_dto(flt, take=10, skip=0)

    assert flt["typeIn"] == {"values": [PURCHASE_TYPE_AUCTION]}
    assert flt["nameLike"] == {"value": "сайт", "contains": True}
    assert flt["regionPaths"] == {}
    assert flt["customerInnOrName"] == {"contains": True}
    assert flt["numberLike"] == {"contains": True}
    assert flt["externalNumberLike"] == {"contains": True}
    assert flt["auctionSpecificFilter"] == {
        "stateIdIn": [STATE_AUCTION_ACTIVE],
        "initialDuration": [3, 6, 24],
        "supplierTotalPoints": {},
    }
    assert flt["needSpecificFilter"] == {"supplierTotalPoints": {}}
    assert flt["tenderSpecificFilter"] == {}
    assert flt["ptkrSpecificFilter"] == {}
    assert dto["order"] == [{"field": "relevance", "desc": True}]
    assert dto["withCount"] is True
    assert dto["take"] == 10
    assert dto["skip"] == 0
    assert PAGE_SIZE == 10


def test_optional_price_and_region_filters_are_preserved():
    flt = _build_filter("сайт", 20_000, 590_000, [".1.504."])

    assert flt["startPriceGreatEqual"] == 20_000
    assert flt["startPriceLessEqual"] == 590_000
    assert flt["regionPaths"] == {"values": [".1.504."]}
