import decision_aid
import database
import document_processor
import filter_engine
import scraper


def test_dom_parser_extracts_stage_security_and_support():
    html = """
    <div class="blockInfo">
      <h2 class="blockInfo__title">Общая информация о закупке</h2>
      <section class="blockInfo__section">
        <span class="section__title">Этап закупки</span>
        <span class="section__info">Подача заявок</span>
      </section>
    </div>
    <div class="blockInfo">
      <h2 class="blockInfo__title">Обеспечение исполнения контракта</h2>
      <section class="blockInfo__section">
        <span class="section__title">Требуется обеспечение исполнения контракта</span>
        <span class="section__info">Да</span>
      </section>
      <section class="blockInfo__section">
        <span class="section__title">Размер обеспечения исполнения контракта</span>
        <span class="section__info">5 %</span>
      </section>
    </div>
    <div class="blockInfo">
      <h2 class="blockInfo__title">Информация о банковском и (или) казначейском сопровождении контракта</h2>
      <section class="blockInfo__section">
        <span class="section__info">Банковское или казначейское сопровождение контракта не требуется</span>
      </section>
    </div>
    """

    details = scraper.parse_common_info_details_from_html(html, price=373104)

    assert details["common"]["purchase_stage"] == "Подача заявок"
    assert details["contract_security"]["required"] is True
    assert details["contract_security"]["raw_value"] == "5 %"
    assert details["contract_security"]["percent"] == 5
    assert details["contract_security"]["amount"] == 18655.2
    assert details["contract_support"]["required"] is False


def test_treasury_support_not_required_does_not_stop_f8():
    text = filter_engine._normalize(
        "Информация о банковском и казначейском сопровождении контракта. "
        "Банковское или казначейское сопровождение контракта не требуется."
    )

    score = filter_engine._filter_contract_risks({}, text, "stage2")

    assert score.stop_factor is False
    assert not any("казначейское сопровождение" in signal for signal in score.signals)


def test_treasury_support_required_still_stops_f8():
    text = filter_engine._normalize("Казначейское сопровождение контракта требуется.")

    score = filter_engine._filter_contract_risks({}, text, "stage2")

    assert score.stop_factor is True
    assert any("казначейское сопровождение" in signal for signal in score.signals)


def test_detail_rejected_is_not_closed_status():
    assert decision_aid._gate_status({"status": "detail_rejected"}) is None


def test_finance_gate_reads_contract_security_from_details_json():
    gate = decision_aid._gate_finance_load({
        "price": 373104,
        "contract_security_amount": None,
        "details_json": {"contract_security": {"amount": 18655.2, "percent": 5}},
    })

    assert gate["status"] == "ok"
    assert "18 655" in gate["explain"]


def test_deadline_is_expired_for_old_procurement():
    now = database.datetime(2026, 6, 12, 12, 0, tzinfo=database.timezone.utc)

    assert database.deadline_is_expired("13.02.2014", now=now) is True


def test_date_only_deadline_stays_active_until_end_of_day():
    noon = database.datetime(2026, 6, 12, 12, 0, tzinfo=database.timezone.utc)
    next_day = database.datetime(2026, 6, 13, 0, 1, tzinfo=database.timezone.utc)

    assert database.deadline_is_expired("12.06.2026", now=noon) is False
    assert database.deadline_is_expired("12.06.2026", now=next_day) is True


def test_work_scope_preserves_docx_tables(tmp_path):
    from docx import Document

    path = tmp_path / "Описание объекта закупки.docx"
    doc = Document()
    doc.add_paragraph("Описание объекта закупки")
    doc.add_paragraph("Оказание услуг по сопровождению системы")
    doc.add_paragraph("Таблица 1 – Объекты предоставления услуг:")
    table = doc.add_table(rows=1, cols=3)
    table.rows[0].cells[0].text = "№ п/п"
    table.rows[0].cells[1].text = "Тип объекта"
    table.rows[0].cells[2].text = "Количество"
    for idx in range(1, 13):
        row = table.add_row().cells
        row[0].text = str(idx)
        row[1].text = f"Коммутатор {idx}"
        row[2].text = "1 шт."
    doc.add_paragraph("Используемые термины и сокращения")
    terms = doc.add_table(rows=2, cols=2)
    terms.rows[0].cells[0].text = "Термин"
    terms.rows[0].cells[1].text = "Определение"
    terms.rows[1].cells[0].text = "Заявка"
    terms.rows[1].cells[1].text = "Обращение Заказчика"
    doc.save(path)

    scope = document_processor.extract_work_scope_from_files([path])

    assert len(scope["tables"]) == 1
    assert scope["tables"][0]["columns"] == ["№ п/п", "Тип объекта", "Количество"]
    assert len(scope["tables"][0]["rows"]) == 12
    assert scope["tables"][0]["rows"][-1] == ["12", "Коммутатор 12", "1 шт."]
