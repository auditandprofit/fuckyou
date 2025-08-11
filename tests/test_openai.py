import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import util.openai as openai


class DummyClient:
    def __init__(self):
        self.params = None

    class _Responses:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **params):
            self.outer.params = params
            return {}

    @property
    def responses(self):
        return self._Responses(self)


def test_temperature_removed_for_o_models(monkeypatch):
    dummy = DummyClient()
    monkeypatch.setattr(openai, "openai_configure_api", lambda: dummy)

    openai.openai_generate_response(messages=[], model="o1-preview")
    assert "temperature" not in dummy.params

    openai.openai_generate_response(messages=[], model="gpt-4")
    assert dummy.params["temperature"] == 0
