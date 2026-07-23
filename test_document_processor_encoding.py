from document_processor import _decode_command_output, _decode_text_bytes


def test_decodes_cp1251_without_losing_cyrillic():
    source = "Техническое задание № 42"
    assert _decode_text_bytes(source.encode("cp1251")) == source


def test_decodes_utf16le_with_bom():
    source = "Описание объекта закупки"
    assert _decode_command_output(source.encode("utf-16")) == source


def test_decodes_utf16le_without_bom():
    source = "Contract 42 — техническое задание"
    assert _decode_text_bytes(source.encode("utf-16le")) == source


def test_decodes_utf8():
    source = "Проект договора"
    assert _decode_text_bytes(source.encode("utf-8")) == source
