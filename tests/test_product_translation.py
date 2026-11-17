import asyncio
import json
from types import SimpleNamespace

from video_subtitle_grounding.config import Settings
from video_subtitle_grounding.translation import ModelArkSubtitleTranslationAdapter


class ProductArk:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.responses = SimpleNamespace(with_raw_response=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        data = json.loads(kwargs["input"][1]["content"][0]["text"])
        if kwargs["text"]["format"]["name"] == "subtitle_product_names":
            if self.fail:
                raise RuntimeError("private content")
            names = []
            for record in data["requested_subtitles"]:
                for name, model, spec in [
                    ("FLOOR ONE i5 Lite", "FLOOR ONE i5", "Lite"),
                    ("FLOOR ONE S10 Stretch Steam", "FLOOR ONE S10", "Stretch Steam"),
                    # Incorrect known-catalog split must not override the dictionary.
                    ("FLOOR ONE S7 Stretch Steam", "FLOOR ONE S7 Stretch", "Steam"),
                ]:
                    if name in record["text"]:
                        names.append({"segment_id": record["segment_id"], "text": name,
                                      "model": model, "spec": spec})
            result = {"products": names}
        else:
            result = {"translations": [{"segment_id": r["segment_id"],
                                         "translated_text": r["text"].replace("Try", "Teste")}
                                        for r in data["requested_subtitles"]]}
        return {"output": [{"content": [{"type": "output_text", "text": json.dumps(result)}]}],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}


def test_inference_new_model_and_spec_are_protected_and_never_catalog_gated():
    records = [{"segment_id": "1", "text": "Try FLOOR ONE i5 Lite and FLOOR ONE S10 Stretch Steam",
                "block_id": "a", "line_index": 0},
               {"segment_id": "2", "text": "Try FLOOR ONE S7 Stretch Steam",
                "block_id": "a", "line_index": 1}]
    client = ProductArk()
    raw_events = []
    adapter = ModelArkSubtitleTranslationAdapter(
        Settings(), client=client, tls_only_logger=lambda *a, **k: raw_events.append(k))
    result = asyncio.run(adapter.translate(records, segment_ids=["1", "2"],
                                           source_language="en", target_language="de"))
    assert len(client.calls) == 2
    assert result["translations"]["1"].startswith("Teste FLOOR ONE i5 Lite")
    assert [m["spec"] for m in result["product_mentions"]["1"]] == ["Lite", "Stretch Steam"]
    assert all(m["catalog_id"] is None for m in result["product_mentions"]["1"])
    assert result["product_mentions"]["2"][0]["model"] == "FLOOR ONE S7"
    assert result["usage"]["total_tokens"] == 30
    assert len(raw_events) == 2
    assert "__PN_" not in json.dumps(result)


def test_recognition_failure_keeps_catalog_and_translation_available():
    record = {"segment_id": "1", "text": "Try FLOOR ONE S7 Stretch Steam",
              "block_id": "a", "line_index": 0}
    client = ProductArk(fail=True)
    adapter = ModelArkSubtitleTranslationAdapter(Settings(), client=client)
    result = asyncio.run(adapter.translate([record], segment_ids=["1"],
                                           source_language="en", target_language="de"))
    assert result["failed_ids"] == []
    assert result["product_mentions"]["1"][0]["spec"] == "Stretch Steam"
    assert result["warnings"]["1"] == ["product_recognition_unavailable"]
    assert len(client.calls) == 3  # two automatic recognition attempts, then translation


def test_automatic_recognition_retry_recovers_unknown_name_before_translation():
    class TransientArk(ProductArk):
        def create(self, **kwargs):
            if not self.calls:
                self.calls.append(kwargs)
                raise RuntimeError("transient failure")
            return super().create(**kwargs)

    client = TransientArk()
    adapter = ModelArkSubtitleTranslationAdapter(Settings(), client=client)
    result = asyncio.run(adapter.translate(
        [{"segment_id": "1", "text": "Try FLOOR ONE S10 Stretch Steam",
          "block_id": "a", "line_index": 0}],
        segment_ids=["1"], source_language="en", target_language="de"))
    assert len(client.calls) == 3
    assert result["product_mentions"]["1"][0]["model"] == "FLOOR ONE S10"
    assert not result["warnings"].get("1")
    assert result["translations"]["1"] == "Teste FLOOR ONE S10 Stretch Steam"


def test_crossline_unknown_spec_and_bounded_context():
    class CrosslineArk(ProductArk):
        def create(self, **kwargs):
            self.calls.append(kwargs)
            return {"output": [{"content": [{"type": "output_text", "text": json.dumps({
                "products": [{"segment_id": "spec", "text": "Ultra Max", "model": "",
                              "spec": "Ultra Max"}]})}]}]}

    client = CrosslineArk()
    records = [{"segment_id": "spec", "text": "Ultra Max", "block_id": "a", "line_index": 1},
               {"segment_id": "model", "text": "FLOOR ONE S7", "block_id": "a", "line_index": 0},
               *[{"segment_id": str(i), "text": "x" * 100, "block_id": "a", "line_index": i + 2}
                 for i in range(100)]]
    adapter = ModelArkSubtitleTranslationAdapter(Settings(), client=client, token_budget=1200)
    result = asyncio.run(adapter.recognize_products(records, segment_ids=["spec"]))
    assert result["product_mentions"]["spec"][0]["context_model"] == "FLOOR ONE S7"
    sent = json.loads(client.calls[0]["input"][1]["content"][0]["text"])
    assert len(json.dumps(sent["context_only"]).encode()) < 1500
    records[1]["excluded"] = True
    assert not asyncio.run(adapter.recognize_products(records, segment_ids=["spec"]))["product_mentions"]["spec"]
