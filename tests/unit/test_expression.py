"""Unit tests for the expression DSL (Doc 07 §3/§9, Doc 12 §2.3)."""

import pytest

from harkeniq.errors import SkillParseError
from harkeniq.models import BooleanOp, Comparison, NotOp
from harkeniq.skills.expression import (
    FieldRef,
    collect_fields,
    evaluate,
    parse,
    tokenize,
)


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


class TestTokenizer:
    def test_simple_tokens(self):
        tokens = tokenize("health == 'Critical'")
        types = [t.type for t in tokens]
        assert types == ["IDENTIFIER", "OPERATOR", "STRING", "EOF"]
        assert tokens[0].value == "health"
        assert tokens[1].value == "=="
        assert tokens[2].value == "Critical"

    def test_all_operators(self):
        for op in ("==", "!=", "<", ">", "<=", ">="):
            tokens = tokenize(f"x {op} 1")
            assert tokens[1].value == op

    def test_numbers(self):
        assert tokenize("x == 42")[2].value == 42
        assert tokenize("x == 3.14")[2].value == 3.14
        assert tokenize("x < -3")[2].value == -3
        assert tokenize("x < -3.5")[2].value == -3.5

    def test_keywords_case_insensitive(self):
        for word in ("AND", "and", "And", "OR", "or", "NOT", "not"):
            tok = tokenize(f"{word}")[0]
            assert tok.type == "KEYWORD"
            assert tok.value == word.upper()

    def test_unterminated_string(self):
        with pytest.raises(SkillParseError, match="unterminated"):
            tokenize("health == 'Critical")

    def test_invalid_character(self):
        with pytest.raises(SkillParseError, match="unexpected character"):
            tokenize("health @ 'Critical'")

    def test_parens_rejected(self):
        # Parentheses are not supported in R1 (Doc 07 §3.3)
        with pytest.raises(SkillParseError):
            tokenize("(health == 'OK')")

    def test_max_length_exceeded(self):
        expr = "x == 1 AND " * 100  # > 1000 chars
        assert len(expr) > 1000
        with pytest.raises(SkillParseError, match="exceeds 1000"):
            tokenize(expr)


# ---------------------------------------------------------------------------
# Parser (Doc 12 §2.3 AST expectations)
# ---------------------------------------------------------------------------


class TestParser:
    def test_simple_comparison(self):
        ast = parse("health == 'Critical'")
        assert ast == Comparison(field="health", operator="==", value="Critical")

    def test_numeric_comparison(self):
        ast = parse("speed_rpm < 2000")
        assert ast == Comparison(field="speed_rpm", operator="<", value=2000)

    def test_and_expression(self):
        ast = parse("health == 'Critical' AND state == 'Enabled'")
        assert isinstance(ast, BooleanOp)
        assert ast.op == "AND"
        assert ast.left == Comparison(field="health", operator="==", value="Critical")
        assert ast.right == Comparison(field="state", operator="==", value="Enabled")

    def test_or_expression(self):
        ast = parse("health == 'Critical' OR health == 'Warning'")
        assert isinstance(ast, BooleanOp)
        assert ast.op == "OR"

    def test_not_expression(self):
        ast = parse("NOT state == 'Absent'")
        assert isinstance(ast, NotOp)
        assert ast.operand == Comparison(field="state", operator="==", value="Absent")

    def test_precedence_and_binds_tighter_than_or(self):
        # a OR b AND c  =>  OR(a, AND(b, c))
        ast = parse("a == 1 OR b == 2 AND c == 3")
        assert isinstance(ast, BooleanOp)
        assert ast.op == "OR"
        assert isinstance(ast.right, BooleanOp)
        assert ast.right.op == "AND"

    def test_field_reference_value(self):
        ast = parse("speed_rpm < threshold_low_critical")
        assert isinstance(ast, Comparison)
        assert isinstance(ast.value, FieldRef)
        assert ast.value == "threshold_low_critical"

    def test_string_literal_is_not_field_ref(self):
        ast = parse("health == 'Critical'")
        assert not isinstance(ast.value, FieldRef)

    def test_boolean_literals(self):
        assert parse("smart_alert == true").value is True
        assert parse("smart_alert == FALSE").value is False

    def test_case_insensitive_keywords(self):
        ast = parse("health == 'OK' and state == 'Enabled'")
        assert isinstance(ast, BooleanOp)
        assert ast.op == "AND"

    def test_dotted_field_ref(self):
        ast = parse("oem_data.power_on_hours > 10000")
        assert ast.field == "oem_data.power_on_hours"

    def test_max_depth_exceeded(self):
        expr = "NOT " * 21 + "x == 1"
        with pytest.raises(SkillParseError, match="depth"):
            parse(expr)

    def test_max_depth_ok_at_limit(self):
        expr = "NOT " * 19 + "x == 1"  # depth 20
        parse(expr)  # must not raise

    def test_missing_operator(self):
        with pytest.raises(SkillParseError, match="operator"):
            parse("health 'Critical'")

    def test_missing_value(self):
        with pytest.raises(SkillParseError):
            parse("health ==")

    def test_trailing_garbage(self):
        with pytest.raises(SkillParseError, match="unexpected token"):
            parse("health == 'OK' state")

    def test_bare_identifier_rejected(self):
        with pytest.raises(SkillParseError):
            parse("health")


# ---------------------------------------------------------------------------
# Evaluator (Doc 07 §3.6 type coercion, Doc 12 §2.3)
# ---------------------------------------------------------------------------


class TestEvaluator:
    def test_eval_true(self):
        ast = parse("health == 'Critical'")
        assert evaluate(ast, {"health": "Critical"}) is True

    def test_eval_false(self):
        ast = parse("health == 'Critical'")
        assert evaluate(ast, {"health": "OK"}) is False

    def test_numeric_operators(self):
        ctx = {"speed_rpm": 1500}
        assert evaluate(parse("speed_rpm < 2000"), ctx) is True
        assert evaluate(parse("speed_rpm > 2000"), ctx) is False
        assert evaluate(parse("speed_rpm <= 1500"), ctx) is True
        assert evaluate(parse("speed_rpm >= 1501"), ctx) is False
        assert evaluate(parse("speed_rpm == 1500"), ctx) is True
        assert evaluate(parse("speed_rpm != 1500"), ctx) is False

    def test_and_or_not(self):
        ctx = {"health": "Critical", "state": "Enabled"}
        assert evaluate(parse("health == 'Critical' AND state == 'Enabled'"), ctx) is True
        assert evaluate(parse("health == 'OK' OR state == 'Enabled'"), ctx) is True
        assert evaluate(parse("NOT state == 'Absent'"), ctx) is True
        assert evaluate(parse("health == 'OK' AND state == 'Enabled'"), ctx) is False

    def test_field_reference_resolution(self):
        # Doc 07 §3.5: threshold resolved from context
        ast = parse("speed_rpm < threshold_low_critical")
        assert evaluate(ast, {"speed_rpm": 400, "threshold_low_critical": 480}) is True
        assert evaluate(ast, {"speed_rpm": 500, "threshold_low_critical": 480}) is False

    def test_none_field_returns_false(self):
        # Missing data never triggers (Doc 07 §3.6)
        ast = parse("missing_field > 100")
        assert evaluate(ast, {"health": "OK"}) is False

    def test_none_value_returns_false(self):
        ast = parse("ecc_correctable_lifetime >= 100")
        assert evaluate(ast, {"ecc_correctable_lifetime": None}) is False

    def test_missing_field_ref_returns_false(self):
        ast = parse("speed_rpm < threshold_low_critical")
        assert evaluate(ast, {"speed_rpm": 400}) is False

    def test_type_mismatch_number_vs_string(self, caplog):
        ast = parse("speed_rpm == 'fast'")
        with caplog.at_level("WARNING"):
            assert evaluate(ast, {"speed_rpm": 9200}) is False
        assert "mismatch" in caplog.text.lower()

    def test_string_ordering_rejected(self, caplog):
        ast = parse("health < 'OK'")
        with caplog.at_level("WARNING"):
            assert evaluate(ast, {"health": "Critical"}) is False

    def test_boolean_equality(self):
        assert evaluate(parse("smart_alert == true"), {"smart_alert": True}) is True
        assert evaluate(parse("smart_alert == false"), {"smart_alert": True}) is False
        assert evaluate(parse("smart_alert != true"), {"smart_alert": False}) is True

    def test_bool_vs_number_mismatch(self, caplog):
        # bool is not silently coerced to int
        with caplog.at_level("WARNING"):
            assert evaluate(parse("smart_alert == 1"), {"smart_alert": True}) is False

    def test_not_of_missing_field_is_true(self):
        # NOT (false) — Doc 07 §9.3 evaluator semantics
        ast = parse("NOT missing == 'x'")
        assert evaluate(ast, {}) is True

    def test_short_circuit_and(self):
        # Right side would mismatch; short-circuit means it is never evaluated
        ast = parse("health == 'OK' AND speed_rpm == 'fast'")
        assert evaluate(ast, {"health": "Critical", "speed_rpm": 1}) is False

    def test_int_float_comparison(self):
        assert evaluate(parse("reading_c > 47"), {"reading_c": 47.5}) is True

    def test_dotted_field_nested_resolution(self):
        ast = parse("oem_data.power_on_hours > 10000")
        assert evaluate(ast, {"oem_data": {"power_on_hours": 20000}}) is True
        assert evaluate(ast, {"oem_data": {}}) is False


# ---------------------------------------------------------------------------
# collect_fields (load-time field validation support)
# ---------------------------------------------------------------------------


class TestCollectFields:
    def test_simple(self):
        assert collect_fields(parse("health == 'Critical'")) == {"health"}

    def test_field_ref_included(self):
        fields = collect_fields(parse("speed_rpm < threshold_low_critical"))
        assert fields == {"speed_rpm", "threshold_low_critical"}

    def test_boolean_and_not(self):
        fields = collect_fields(
            parse("NOT state == 'Absent' AND health == 'OK' OR speed_rpm < 100")
        )
        assert fields == {"state", "health", "speed_rpm"}

    def test_string_literal_not_included(self):
        assert collect_fields(parse("health == 'health'")) == {"health"}
