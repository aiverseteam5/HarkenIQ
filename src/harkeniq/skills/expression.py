"""Expression DSL: tokenizer, parser, evaluator (Doc 07 §3, §9).

Grammar (Doc 07 §3.1):
    expression := or_expr
    or_expr    := and_expr ( 'OR' and_expr )*
    and_expr   := not_expr ( 'AND' not_expr )*
    not_expr   := 'NOT' not_expr | comparison
    comparison := field_ref OPERATOR value
    value      := NUMBER | STRING | BOOLEAN | FIELD_REF

Precedence: NOT > comparisons > AND > OR. No parentheses in R1.
Security (Doc 07 §9.4): no eval/exec, max length 1000, max AST depth 20.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Union

from harkeniq.errors import SkillParseError
from harkeniq.models import ASTNode, BooleanOp, Comparison, NotOp

logger = logging.getLogger("harkeniq.skills.expression")

MAX_EXPRESSION_LENGTH = 1000
MAX_AST_DEPTH = 20

OPERATORS = ("==", "!=", "<=", ">=", "<", ">")
KEYWORDS = ("AND", "OR", "NOT")


class FieldRef(str):
    """Marker for an unquoted identifier on the right side of a comparison.

    Resolved against the sensor context at evaluation time (Doc 07 §3.5:
    threshold references like `speed_rpm < threshold_low_critical`).
    """


# ---------------------------------------------------------------------------
# Tokenizer (Doc 07 §9.1)
# ---------------------------------------------------------------------------

# Token types
IDENTIFIER = "IDENTIFIER"
NUMBER = "NUMBER"
STRING = "STRING"
OPERATOR = "OPERATOR"
KEYWORD = "KEYWORD"
DOT = "DOT"
EOF = "EOF"


@dataclass
class Token:
    type: str
    value: Any
    pos: int


def tokenize(expression: str) -> list[Token]:
    """Convert a condition string into a token list.

    Raises SkillParseError on invalid characters, unterminated strings,
    or expressions longer than MAX_EXPRESSION_LENGTH.
    """
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise SkillParseError(
            expression[:80] + "...",
            f"expression exceeds {MAX_EXPRESSION_LENGTH} characters ({len(expression)})",
        )

    tokens: list[Token] = []
    i = 0
    n = len(expression)

    while i < n:
        c = expression[i]

        if c.isspace():
            i += 1
            continue

        # Two-char operators first
        two = expression[i:i + 2]
        if two in ("==", "!=", "<=", ">="):
            tokens.append(Token(OPERATOR, two, i))
            i += 2
            continue
        if c in ("<", ">"):
            tokens.append(Token(OPERATOR, c, i))
            i += 1
            continue

        if c == ".":
            tokens.append(Token(DOT, ".", i))
            i += 1
            continue

        # String literal (single-quoted; no escape sequences in R1)
        if c == "'":
            end = expression.find("'", i + 1)
            if end == -1:
                raise SkillParseError(expression, f"unterminated string literal at position {i}")
            tokens.append(Token(STRING, expression[i + 1:end], i))
            i = end + 1
            continue

        # Number (int or float, optional leading minus)
        if c.isdigit() or (c == "-" and i + 1 < n and expression[i + 1].isdigit()):
            start = i
            if c == "-":
                i += 1
            while i < n and expression[i].isdigit():
                i += 1
            is_float = False
            if i + 1 < n and expression[i] == "." and expression[i + 1].isdigit():
                is_float = True
                i += 1
                while i < n and expression[i].isdigit():
                    i += 1
            text = expression[start:i]
            tokens.append(Token(NUMBER, float(text) if is_float else int(text), start))
            continue

        # Identifier or keyword
        if c.isalpha() or c == "_":
            start = i
            while i < n and (expression[i].isalnum() or expression[i] == "_"):
                i += 1
            word = expression[start:i]
            if word.upper() in KEYWORDS:
                tokens.append(Token(KEYWORD, word.upper(), start))
            else:
                tokens.append(Token(IDENTIFIER, word, start))
            continue

        raise SkillParseError(expression, f"unexpected character {c!r} at position {i}")

    tokens.append(Token(EOF, None, n))
    return tokens


# ---------------------------------------------------------------------------
# Parser (Doc 07 §9.2) — recursive descent
# ---------------------------------------------------------------------------


class _Parser:
    def __init__(self, expression: str, tokens: list[Token]):
        self.expression = expression
        self.tokens = tokens
        self.pos = 0

    @property
    def current(self) -> Token:
        return self.tokens[self.pos]

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def error(self, detail: str) -> SkillParseError:
        return SkillParseError(self.expression, detail)

    # expression := or_expr
    def parse(self) -> ASTNode:
        node = self.or_expr()
        if self.current.type != EOF:
            raise self.error(
                f"unexpected token {self.current.value!r} at position {self.current.pos}"
            )
        return node

    # or_expr := and_expr ( 'OR' and_expr )*
    def or_expr(self) -> ASTNode:
        node = self.and_expr()
        while self.current.type == KEYWORD and self.current.value == "OR":
            self.advance()
            node = BooleanOp(op="OR", left=node, right=self.and_expr())
        return node

    # and_expr := not_expr ( 'AND' not_expr )*
    def and_expr(self) -> ASTNode:
        node = self.not_expr()
        while self.current.type == KEYWORD and self.current.value == "AND":
            self.advance()
            node = BooleanOp(op="AND", left=node, right=self.not_expr())
        return node

    # not_expr := 'NOT' not_expr | comparison
    def not_expr(self) -> ASTNode:
        if self.current.type == KEYWORD and self.current.value == "NOT":
            self.advance()
            return NotOp(operand=self.not_expr())
        return self.comparison()

    # comparison := field_ref OPERATOR value
    def comparison(self) -> ASTNode:
        field = self.field_ref()
        if self.current.type != OPERATOR:
            raise self.error(
                f"expected comparison operator after {field!r}, got {self.current.value!r}"
            )
        operator = self.advance().value
        value = self.value()
        return Comparison(field=field, operator=operator, value=value)

    # field_ref := IDENTIFIER ( '.' IDENTIFIER )*
    def field_ref(self) -> str:
        if self.current.type != IDENTIFIER:
            raise self.error(
                f"expected field name, got {self.current.value!r} "
                f"at position {self.current.pos}"
            )
        parts = [self.advance().value]
        while self.current.type == DOT:
            self.advance()
            if self.current.type != IDENTIFIER:
                raise self.error("expected identifier after '.'")
            parts.append(self.advance().value)
        return ".".join(parts)

    # value := NUMBER | STRING | BOOLEAN | FIELD_REF
    def value(self) -> Any:
        tok = self.current
        if tok.type == NUMBER:
            self.advance()
            return tok.value
        if tok.type == STRING:
            self.advance()
            return tok.value
        if tok.type == IDENTIFIER:
            if tok.value.lower() in ("true", "false"):
                self.advance()
                return tok.value.lower() == "true"
            return FieldRef(self.field_ref())
        raise self.error(f"expected value, got {tok.value!r} at position {tok.pos}")


def _depth(node: ASTNode) -> int:
    if isinstance(node, Comparison):
        return 1
    if isinstance(node, NotOp):
        return 1 + _depth(node.operand)
    if isinstance(node, BooleanOp):
        return 1 + max(_depth(node.left), _depth(node.right))
    return 1


def parse(expression: str) -> ASTNode:
    """Parse a condition string into an AST.

    Raises SkillParseError for invalid syntax, over-long expressions,
    or ASTs deeper than MAX_AST_DEPTH.
    """
    tokens = tokenize(expression)
    ast = _Parser(expression, tokens).parse()
    depth = _depth(ast)
    if depth > MAX_AST_DEPTH:
        raise SkillParseError(
            expression[:80], f"AST depth {depth} exceeds maximum {MAX_AST_DEPTH}"
        )
    return ast


def collect_fields(node: ASTNode) -> set[str]:
    """Return all field names referenced by an AST (left sides + field refs).

    Used by the skill loader for load-time field validation.
    """
    if isinstance(node, Comparison):
        fields = {node.field}
        if isinstance(node.value, FieldRef):
            fields.add(str(node.value))
        return fields
    if isinstance(node, NotOp):
        return collect_fields(node.operand)
    if isinstance(node, BooleanOp):
        return collect_fields(node.left) | collect_fields(node.right)
    return set()


# ---------------------------------------------------------------------------
# Evaluator (Doc 07 §9.3) — type coercion per Doc 07 §3.6
# ---------------------------------------------------------------------------


def _resolve(context: dict, name: str) -> Any:
    """Resolve a (possibly dotted) field name against the context."""
    if name in context:
        return context[name]
    if "." in name:
        obj: Any = context
        for part in name.split("."):
            if isinstance(obj, dict) and part in obj:
                obj = obj[part]
            else:
                return None
        return obj
    return None


def _compare(left: Any, operator: str, right: Any, field: str) -> bool:
    """Apply type coercion rules (Doc 07 §3.6)."""
    # bool is a subclass of int — check booleans first
    if isinstance(left, bool) or isinstance(right, bool):
        if isinstance(left, bool) and isinstance(right, bool):
            if operator == "==":
                return left == right
            if operator == "!=":
                return left != right
            logger.warning(
                "Operator %s not valid for booleans (field %s); condition is false",
                operator, field,
            )
            return False
        logger.warning(
            "Type mismatch on field %s: %s %s %s; condition is false",
            field, type(left).__name__, operator, type(right).__name__,
        )
        return False

    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if operator == "==":
            return left == right
        if operator == "!=":
            return left != right
        if operator == "<":
            return left < right
        if operator == ">":
            return left > right
        if operator == "<=":
            return left <= right
        if operator == ">=":
            return left >= right

    if isinstance(left, str) and isinstance(right, str):
        if operator == "==":
            return left == right
        if operator == "!=":
            return left != right
        logger.warning(
            "Operator %s not valid for strings (field %s); condition is false",
            operator, field,
        )
        return False

    logger.warning(
        "Type mismatch on field %s: %s %s %s; condition is false",
        field, type(left).__name__, operator, type(right).__name__,
    )
    return False


def evaluate(node: ASTNode, context: dict) -> bool:
    """Evaluate an AST node against a sensor context dict.

    Missing fields (None) never trigger a verdict (Doc 07 §3.6).
    AND/OR short-circuit.
    """
    if isinstance(node, Comparison):
        left = _resolve(context, node.field)
        right = node.value
        if isinstance(right, FieldRef):
            right = _resolve(context, str(right))
        if left is None or right is None:
            return False  # Missing data never triggers
        return _compare(left, node.operator, right, node.field)

    if isinstance(node, BooleanOp):
        if node.op == "AND":
            return evaluate(node.left, context) and evaluate(node.right, context)
        return evaluate(node.left, context) or evaluate(node.right, context)

    if isinstance(node, NotOp):
        return not evaluate(node.operand, context)

    raise TypeError(f"Unknown AST node type: {type(node).__name__}")
