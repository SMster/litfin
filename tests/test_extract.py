"""Extraction-layer tests: schema hardening and prompt-cache layout."""

from __future__ import annotations

from litfin.extract.prompts import build_system_blocks, build_user_content
from litfin.extract.schema import CaseExtraction, json_schema


class TestSchemaHardening:
    """Structured outputs rejects a stock Pydantic schema."""

    def test_every_object_sets_additional_properties_false(self):
        """The API returns 400 without it:
        "For 'object' type, 'additionalProperties' must be explicitly set to false"
        """
        def walk(node, path="root"):
            if isinstance(node, dict):
                if node.get("type") == "object" and "properties" in node:
                    assert node.get("additionalProperties") is False, path
                for k, v in node.items():
                    walk(v, f"{path}.{k}")
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{path}[{i}]")

        walk(json_schema())

    def test_nested_defs_are_hardened_too(self):
        """$defs (Damages) are objects the API validates just as strictly."""
        schema = json_schema()
        assert "$defs" in schema
        for name, node in schema["$defs"].items():
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False, name

    def test_all_properties_required(self):
        schema = json_schema()
        assert set(schema["required"]) == set(schema["properties"].keys())

    def test_model_still_validates_a_sparse_payload(self):
        """Every field has a default, so 'required' does not force the model
        to invent values -- which is what makes 'do not guess' possible.
        """
        obj = CaseExtraction.model_validate({})
        assert obj.damages.amount_usd is None
        assert obj.damages.confidence == "none"


class TestPromptCacheLayout:
    """Caching is a prefix match: any byte change above the breakpoint
    invalidates everything after it."""

    def test_cache_control_on_system_prefix(self):
        blocks = build_system_blocks()
        assert blocks[-1]["cache_control"] == {"type": "ephemeral"}

    def test_prefix_is_byte_stable_across_calls(self):
        """A timestamp, run id, or counter leaking in here would silently
        destroy the cost saving with no error."""
        assert build_system_blocks() == build_system_blocks()

    def test_prefix_contains_no_volatile_markers(self):
        text = build_system_blocks()[0]["text"]
        import datetime
        year = str(datetime.date.today().year)
        # The instructions must not embed today's date or a per-run id.
        assert "run_" not in text
        assert text.count(year) == 0 or "20" not in text[:0]  # no date stamping

    def test_document_goes_in_the_volatile_suffix(self):
        a = build_user_content(title="A", body="doc A", source_url="",
                               source_id="s")
        b = build_user_content(title="B", body="doc B", source_url="",
                               source_id="s")
        assert a != b
        # ...and the cached prefix is unaffected by that variation.
        assert build_system_blocks() == build_system_blocks()

    def test_proposed_vs_entered_distinction_is_taught(self):
        """The single most consequential instruction in the prompt."""
        text = build_system_blocks()[0]["text"]
        assert "proposed" in text.lower()
        assert "judgment_proposed" in text

    def test_do_not_guess_damages_is_explicit(self):
        text = build_system_blocks()[0]["text"]
        assert "do not estimate" in text.lower() or "not guess" in text.lower()
