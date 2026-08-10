from web_app import zakupki_common_info_url, zakupki_documents_url


def test_spb_estore_link_is_not_rewritten_as_eis_notice():
    source = "https://estore.gz-spb.ru/electronicshop/procedure/form/view/795436/"
    purchase_number = "SPB-2026006146451"

    assert zakupki_common_info_url(source, purchase_number) == source
    assert zakupki_documents_url(source, purchase_number) == source


def test_other_external_link_with_long_number_is_preserved():
    source = "https://example.test/tender/0123456789012"

    assert zakupki_common_info_url(source, "EXT-0123456789012") == source
    assert zakupki_documents_url(source, "EXT-0123456789012") == source


def test_eis_link_is_still_normalized_to_common_info_and_documents():
    source = (
        "https://zakupki.gov.ru/epz/order/notice/zk20/view/common-info.html"
        "?regNumber=0123456789012"
    )

    assert zakupki_common_info_url(source) == source
    assert zakupki_documents_url(source) == (
        "https://zakupki.gov.ru/epz/order/notice/zk20/view/documents.html"
        "?regNumber=0123456789012"
    )


def test_numeric_purchase_number_without_source_still_gets_eis_link():
    assert zakupki_common_info_url("", "0123456789012") == (
        "https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html"
        "?regNumber=0123456789012"
    )
