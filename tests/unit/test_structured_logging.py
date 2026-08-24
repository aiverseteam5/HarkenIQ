"""Tests for structured logging + request-id (R4-0 Phase 3)."""

from __future__ import annotations

import json
import logging

import pytest

from harkeniq.logging_config import (
    JSONFormatter,
    configure_logging,
    generate_request_id,
    get_request_id,
    request_id_var,
    set_request_id,
)


class TestJSONFormatter:
    def test_formats_as_json(self):
        formatter = JSONFormatter(service="sm")
        record = logging.LogRecord(
            name="harkeniq.sm", level=logging.INFO,
            pathname="", lineno=0, msg="test message",
            args=None, exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["msg"] == "test message"
        assert data["level"] == "INFO"
        assert data["service"] == "sm"
        assert "ts" in data

    def test_includes_request_id_when_set(self):
        token = request_id_var.set("test-rid-123")
        try:
            formatter = JSONFormatter()
            record = logging.LogRecord(
                name="test", level=logging.INFO,
                pathname="", lineno=0, msg="hello",
                args=None, exc_info=None,
            )
            output = formatter.format(record)
            data = json.loads(output)
            assert data["request_id"] == "test-rid-123"
        finally:
            request_id_var.reset(token)

    def test_no_request_id_when_empty(self):
        token = request_id_var.set("")
        try:
            formatter = JSONFormatter()
            record = logging.LogRecord(
                name="test", level=logging.INFO,
                pathname="", lineno=0, msg="hello",
                args=None, exc_info=None,
            )
            output = formatter.format(record)
            data = json.loads(output)
            assert "request_id" not in data
        finally:
            request_id_var.reset(token)


class TestRequestId:
    def test_generate_is_12_chars(self):
        rid = generate_request_id()
        assert len(rid) == 12

    def test_generate_is_unique(self):
        ids = {generate_request_id() for _ in range(100)}
        assert len(ids) == 100

    def test_set_and_get(self):
        token = request_id_var.set("")
        try:
            set_request_id("my-request-123")
            assert get_request_id() == "my-request-123"
        finally:
            request_id_var.reset(token)


class TestConfigureLogging:
    def test_configure_json(self):
        configure_logging(service="test", level="DEBUG", json_output=True)
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JSONFormatter)

    def test_configure_text(self):
        configure_logging(service="test", level="INFO", json_output=False)
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert not isinstance(root.handlers[0].formatter, JSONFormatter)
