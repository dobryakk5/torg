from database import _feedback_topics


def test_feedback_topics_groups_rejected_titles_and_keeps_examples():
    rows = [
        {"purchase_number": "1", "title": "Разработка проектной документации"},
        {"purchase_number": "2", "title": "Переустройство помещения"},
        {"purchase_number": "3", "title": "Техническое обслуживание автомобиля"},
        {"purchase_number": "4", "title": "Разработка информационной системы"},
    ]

    topics = _feedback_topics(rows)

    assert topics[0]["name"] == "Строительство и проектирование"
    assert topics[0]["count"] == 2
    assert [item["purchase_number"] for item in topics[0]["examples"]] == ["1", "2"]
    assert topics[1]["name"] == "Автомобили и транспорт"
    assert topics[1]["count"] == 1
    assert sum(topic["count"] for topic in topics) == 3


def test_feedback_topics_limits_examples_to_three():
    rows = [
        {"purchase_number": str(i), "title": f"Строительство объекта {i}"}
        for i in range(5)
    ]

    topics = _feedback_topics(rows)

    assert topics[0]["count"] == 5
    assert len(topics[0]["examples"]) == 3
