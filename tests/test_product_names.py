from video_subtitle_grounding.product_names import (
    PRODUCT_CATALOG, catalog_mentions, effective_mentions, protect_names,
    restore_names, text_fingerprint, validated_mentions,
)


def test_pdf_catalog_and_boundaries():
    assert len(PRODUCT_CATALOG) == 72
    assert len({entry["catalog_id"] for entry in PRODUCT_CATALOG}) == 72
    for entry in PRODUCT_CATALOG:
        found = catalog_mentions(entry["text"])
        assert len(found) == 1
        assert found[0]["model"] == entry["model"]
        assert found[0]["spec"] == entry["spec"]
    assert catalog_mentions("Steam Pro Stretch") == []
    assert catalog_mentions("XA11 Hero") == []
    assert catalog_mentions("FLOOR ONE STRETCH S6")[0]["spec"] == ""
    assert catalog_mentions("floor  one S7 Stretch Steam")[0]["spec"] == "Stretch Steam"


def test_unknown_structure_not_membership_and_emoji():
    text = "😀 FLOOR ONE S10 Stretch Steam!"
    item = {"start": 2, "end": len(text) - 1, "model": "FLOOR ONE S10",
            "spec": "Stretch Steam", "origin": "model_pattern", "catalog_id": None}
    assert validated_mentions(text, [item])[0]["catalog_id"] is None
    assert not validated_mentions(text, [{**item, "model": "FLOOR ONE S7"}])
    segment = {"translated_text": text, "product_mentions": [item],
               "product_text_sha256": text_fingerprint(text)}
    assert effective_mentions(segment)[0]["model"] == "FLOOR ONE S10"
    assert effective_mentions({**segment, "translated_text": "Changed"}) == []
    assert effective_mentions({**segment, "product_names_enabled": False}) == []


def test_unknown_spec_extends_known_model_and_protection():
    text = "Try FLOOR ONE i5 Lite and FLOOR ONE S7 Stretch Steam"
    first = {"start": 4, "end": 4 + len("FLOOR ONE i5 Lite"), "model": "FLOOR ONE i5",
             "spec": "Lite", "origin": "model_pattern"}
    mentions = effective_mentions({"translated_text": text, "product_mentions": [first],
                                  "product_text_sha256": text_fingerprint(text)})
    assert [m["spec"] for m in mentions] == ["Lite", "Stretch Steam"]
    protected, mapping = protect_names(text, mentions)
    restored, spans, warnings = restore_names(protected.replace("Try", "Teste"), mapping)
    assert restored.startswith("Teste FLOOR ONE i5 Lite")
    assert len(spans) == 2
    assert warnings == []
    broken, _, warnings = restore_names("No names", mapping)
    assert "__PN_" not in broken
    assert "product_name_missing_after_translation" in warnings


def test_spec_line_requires_current_enabled_block_model():
    from video_subtitle_grounding.product_names import contextual_segments

    model = {"segment_id": "model", "block_id": "a", "translated_text": "FLOOR ONE S7"}
    spec = {"segment_id": "spec", "block_id": "a", "translated_text": "Stretch Steam",
            "product_text_sha256": text_fingerprint("Stretch Steam"), "product_mentions": [{
                "start": 0, "end": 13, "model": "", "spec": "Stretch Steam",
                "context_model": "FLOOR ONE S7", "origin": "model_pattern"}]}
    assert contextual_segments([model, spec])[1]["product_mentions"]
    for change in ({"translated_text": "Changed"}, {"excluded": True},
                   {"product_names_enabled": False}, {"block_id": "other"}):
        assert not contextual_segments([{**model, **change}, spec])[1]["product_mentions"]
    assert not contextual_segments([{**model, "block_id": None},
                                   {**spec, "block_id": None}])[1]["product_mentions"]


def test_broken_or_repeated_markers_warn_and_never_expose_internal_ids():
    protected, mapping = protect_names("FLOOR ONE S7 Stretch Steam",
                                      catalog_mentions("FLOOR ONE S7 Stretch Steam"))
    text, _, warnings = restore_names(protected + " " + protected, mapping)
    assert "__PN_" not in text
    assert "product_name_marker_invalid" in warnings
