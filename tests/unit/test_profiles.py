"""Unit tests for profile loading and variable validation."""

import pytest
from tower.profiles import _validate_vars


class TestValidateVars:
    """Test typed variable validation and defaults."""

    def test_legacy_plain_value(self):
        defs = {"key": "default_val"}
        result = _validate_vars(defs, {}, "test")
        assert result["key"] == "default_val"

    def test_legacy_override(self):
        defs = {"key": "default_val"}
        result = _validate_vars(defs, {"key": "custom"}, "test")
        assert result["key"] == "custom"

    def test_typed_string_default(self):
        defs = {"name": {"type": "string", "default": "hello"}}
        result = _validate_vars(defs, {}, "test")
        assert result["name"] == "hello"

    def test_typed_string_override(self):
        defs = {"name": {"type": "string", "default": "hello"}}
        result = _validate_vars(defs, {"name": "world"}, "test")
        assert result["name"] == "world"

    def test_typed_required_missing(self):
        defs = {"name": {"type": "string", "required": True}}
        with pytest.raises(ValueError, match="required"):
            _validate_vars(defs, {}, "test")

    def test_typed_required_provided(self):
        defs = {"name": {"type": "string", "required": True}}
        result = _validate_vars(defs, {"name": "ok"}, "test")
        assert result["name"] == "ok"

    def test_typed_integer(self):
        defs = {"count": {"type": "integer", "default": 5}}
        result = _validate_vars(defs, {"count": 10}, "test")
        assert result["count"] == 10

    def test_typed_integer_wrong_type(self):
        defs = {"count": {"type": "integer", "default": 5}}
        with pytest.raises(ValueError, match="expected integer"):
            _validate_vars(defs, {"count": "not_a_number"}, "test")

    def test_typed_boolean(self):
        defs = {"flag": {"type": "boolean", "default": False}}
        result = _validate_vars(defs, {"flag": True}, "test")
        assert result["flag"] is True

    def test_typed_boolean_wrong_type(self):
        defs = {"flag": {"type": "boolean", "default": False}}
        with pytest.raises(ValueError, match="expected boolean"):
            _validate_vars(defs, {"flag": "yes"}, "test")

    def test_enum_valid(self):
        defs = {"lang": {"type": "string", "enum": ["Python", "Go", "Rust"], "default": "Python"}}
        result = _validate_vars(defs, {"lang": "Go"}, "test")
        assert result["lang"] == "Go"

    def test_enum_invalid(self):
        defs = {"lang": {"type": "string", "enum": ["Python", "Go", "Rust"], "default": "Python"}}
        with pytest.raises(ValueError, match="must be one of"):
            _validate_vars(defs, {"lang": "Java"}, "test")

    def test_extra_vars_passed_through(self):
        defs = {"name": {"type": "string", "default": "hello"}}
        result = _validate_vars(defs, {"name": "ok", "extra": "value"}, "test")
        assert result["name"] == "ok"
        assert result["extra"] == "value"

    def test_empty_defs_empty_provided(self):
        result = _validate_vars({}, {}, "test")
        assert result == {}

    def test_float_accepts_int(self):
        defs = {"price": {"type": "float", "default": 1.0}}
        result = _validate_vars(defs, {"price": 5}, "test")
        assert result["price"] == 5
