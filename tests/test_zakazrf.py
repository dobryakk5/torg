from sources.zakazrf import _parse_html


HTML = """
<html><head><meta charset="UTF-8"></head><body>
<table class="reporttable" objecttype="DeliveryRequest" loadedpage="1" pagecount="1" totalrows="1">
  <tr>
    <th>Торговый режим</th><th>Номер</th><th>Артикул</th><th>КТРУ+/КСР+</th>
    <th>Название позиции</th><th>Единица измерения</th><th>Цена за единицу</th>
    <th>Количество</th><th>Начальная цена</th><th>Организатор</th>
    <th>Регион поставки</th><th>Дата начала подачи предложений</th>
    <th>Окончание определения лучшего предложения</th><th>Место поставки</th>
    <th>Дата планируемой поставки (по договору)</th><th>Статус торгов</th>
    <th>Статус извещения</th>
  </tr>
  <tr>
    <td>48 часов</td><td><a href="https://bp.zakazrf.ru/DeliveryRequest/id/1827785">BP01549364</a></td>
    <td></td><td>62.01.10.000-00000003-00005</td><td>Выполнение работ по доработке сайта</td>
    <td></td><td></td><td></td><td>12 000</td><td>Кировская ветлаборатория</td>
    <td>Кировская область</td><td>16.07.2099 03:00 (+03:00)</td>
    <td>20.07.2099 15:54 (+03:00)</td><td>г. Киров</td><td></td>
    <td>Опубликовано</td><td>Идут торги</td>
  </tr>
</table></body></html>
"""


def test_parse_delivery_request_table():
    rows = _parse_html(HTML.encode("utf-8"), keyword="сайт")

    assert len(rows) == 1
    row = rows[0]
    assert row["purchase_number"] == "ZAKAZRF-1827785"
    assert row["title"] == "Выполнение работ по доработке сайта"
    assert row["price"] == 12_000
    assert row["customer"] == "Кировская ветлаборатория"
    assert row["region"] == "Кировская область"
    assert row["published_at"] == "16.07.2099 03:00"
    assert row["deadline"] == "20.07.2099 15:54"
    assert row["url"] == "https://bp.zakazrf.ru/DeliveryRequest/id/1827785"
    assert row["platform"] == "БП ZakazRF"
    assert row["matched_keywords"] == ["zakazrf:сайт"]
    assert "Адрес исполнения: г. Киров" in row["primary_text"]
    assert "Место поставки" not in row["primary_text"]


def test_parse_skips_inactive_rows():
    html = HTML.replace("Идут торги", "Завершен")
    assert _parse_html(html, keyword="сайт") == []
