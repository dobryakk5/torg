from unittest.mock import patch

import knowledge_base as kb


def test_active_risk_rules_are_cached_and_invalidated():
    rules = [{"pattern": "пример", "rule_type": "warn", "weight": -1}]
    kb.invalidate_profile_cache()

    with patch.object(kb.db, "kb_risk_rules_active", return_value=rules) as load:
        assert kb._active_risk_rules() == rules
        assert kb._active_risk_rules() == rules
        load.assert_called_once_with()

        kb.invalidate_profile_cache()
        assert kb._active_risk_rules() == rules
        assert load.call_count == 2
