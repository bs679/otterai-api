"""Offline tests for the transcript endpoint and session persistence.

These run without Otter credentials or network: the requests session is
replaced with a stub. The live-credential tests stay in test_otterai.py.
"""

import json

import pytest
import requests

from otterai.otterai import OtterAI, OtterAIException


class StubResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data if data is not None else {}

    def json(self):
        return self._data


class StubSession:
    """Records get() calls and returns the queued responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.cookies = requests.cookies.cookiejar_from_dict({})

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": params or {}})
        return self.responses.pop(0)


def test_get_speech_transcript():
    otter = OtterAI()
    otter._userid = "user-1"
    segments = {"transcripts": [{"transcript": "hello world", "speaker_id": 7}]}
    otter._session = StubSession([StubResponse(200, segments)])

    response = otter.get_speech_transcript("speech-otid-1")

    assert response["status"] == 200
    assert response["data"] == segments
    call = otter._session.calls[0]
    assert call["url"].endswith("/transcripts")
    assert call["params"] == {"userid": "user-1", "otid": "speech-otid-1"}


def test_get_speech_transcript_invalid_userid():
    otter = OtterAI()
    with pytest.raises(OtterAIException, match="userid is invalid"):
        otter.get_speech_transcript("speech-otid-1")


def test_save_and_load_session(tmp_path):
    path = tmp_path / "session.json"
    otter = OtterAI()
    otter._userid = "user-42"
    otter._session.cookies.set("csrftoken", "tok123")
    otter.save_session(str(path))

    saved = json.loads(path.read_text())
    assert saved["userid"] == "user-42"
    assert saved["cookies"]["csrftoken"] == "tok123"

    restored = OtterAI()
    assert restored.load_session(str(path)) is True
    assert restored._userid == "user-42"
    assert restored._cookies["csrftoken"] == "tok123"
    assert restored._session.cookies.get("csrftoken") == "tok123"
    assert restored._is_userid_invalid() is False


def test_load_session_missing_file(tmp_path):
    otter = OtterAI()
    assert otter.load_session(str(tmp_path / "absent.json")) is False
    assert otter._is_userid_invalid() is True


def test_load_session_rejects_empty_userid(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"userid": "", "cookies": {}}))
    otter = OtterAI()
    assert otter.load_session(str(path)) is False


def test_is_session_valid_true():
    otter = OtterAI()
    otter._session = StubSession([StubResponse(200, {"userid": "user-1"})])
    assert otter.is_session_valid() is True


def test_is_session_valid_false_on_401():
    otter = OtterAI()
    otter._session = StubSession([StubResponse(401, {})])
    assert otter.is_session_valid() is False
