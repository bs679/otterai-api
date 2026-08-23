"""Offline tests for the MCP connector (otterai_mcp.server).

These run without Otter credentials or network: the OtterAI client's
requests session is replaced with the same stub used by test_offline.py,
and the module-level cached client is injected directly.
"""

import json

import pytest

pytest.importorskip("mcp")

from otterai.otterai import OtterAI

from otterai_mcp import server
from tests.test_offline import StubResponse, StubSession


@pytest.fixture(autouse=True)
def clean_client(monkeypatch, tmp_path):
    """Isolate every test from cached clients, env credentials and any
    real session file on the machine running the tests."""
    monkeypatch.delenv("OTTERAI_USERNAME", raising=False)
    monkeypatch.delenv("OTTERAI_PASSWORD", raising=False)
    monkeypatch.setenv("OTTERAI_SESSION_FILE", str(tmp_path / "session.json"))
    server._reset_client()
    yield
    server._reset_client()


def stub_client(responses):
    otter = OtterAI()
    otter._userid = "user-1"
    otter._session = StubSession(responses)
    server._client = otter
    return otter


SPEECH = {
    "otid": "otid-abc",
    "title": "Weekly sync",
    "created_at": 1755000000,
    "duration": 1830,
    "owner": {"name": "Dave"},
    "summary": "Discussed roadmap and hiring.",
    "speakers": [{"id": 7, "speaker_name": "Alice"}],
}


def test_requires_credentials_when_no_session():
    result = server.otterai_list_recordings(server.ListRecordingsInput())
    assert result.startswith("Error: Not authenticated")
    assert "OTTERAI_USERNAME" in result


def test_list_recordings_markdown():
    stub_client([StubResponse(200, {"speeches": [SPEECH], "end_of_list": True})])

    result = server.otterai_list_recordings(server.ListRecordingsInput())

    assert "Weekly sync" in result
    assert "`otid-abc`" in result
    assert "30m 30s" in result
    assert "Discussed roadmap and hiring." in result


def test_list_recordings_json_and_params():
    otter = stub_client(
        [StubResponse(200, {"speeches": [SPEECH], "end_of_list": False})]
    )

    result = server.otterai_list_recordings(
        server.ListRecordingsInput(
            folder=3, page_size=5, source="shared", response_format="json"
        )
    )

    payload = json.loads(result)
    assert payload["count"] == 1
    assert payload["end_of_list"] is False
    assert payload["recordings"][0]["speech_id"] == "otid-abc"
    call = otter._session.calls[0]
    assert call["params"]["folder"] == 3
    assert call["params"]["page_size"] == 5
    assert call["params"]["source"] == "shared"


def test_get_recording_markdown_includes_summary_and_speakers():
    stub_client([StubResponse(200, {"speech": SPEECH})])

    result = server.otterai_get_recording(
        server.GetRecordingInput(speech_id="otid-abc")
    )

    assert result.startswith("# Weekly sync")
    assert "## Summary" in result
    assert "Discussed roadmap and hiring." in result
    assert "Alice" in result


def test_get_summary_prefers_abstract_summary():
    speech = dict(SPEECH)
    speech["abstract_summary"] = {
        "short_summary": "Roadmap review.",
        "items": ["Ship v2", "Hire two engineers"],
    }
    stub_client([StubResponse(200, {"speech": speech})])

    result = server.otterai_get_summary(
        server.GetRecordingInput(speech_id="otid-abc", response_format="json")
    )

    payload = json.loads(result)
    assert payload["title"] == "Weekly sync"
    assert "Roadmap review." in payload["summary"]
    assert "- Ship v2" in payload["summary"]


def test_get_summary_missing():
    speech = {"otid": "otid-abc", "title": "Weekly sync"}
    stub_client([StubResponse(200, {"speech": speech})])

    result = server.otterai_get_summary(server.GetRecordingInput(speech_id="otid-abc"))

    assert "No summary available" in result


def test_get_transcript_markdown_with_speaker_names():
    segments = {
        "transcripts": [
            {"transcript": "hello world", "speaker_id": 7, "start_offset": 65000},
            {"transcript": "hi there", "speaker_id": 9, "start_offset": 125000},
        ]
    }
    stub_client([StubResponse(200, segments), StubResponse(200, {"speech": SPEECH})])

    result = server.otterai_get_transcript(
        server.GetTranscriptInput(speech_id="otid-abc")
    )

    assert "# Transcript: Weekly sync" in result
    assert "**Alice** [1:05]: hello world" in result
    assert "**Speaker 9** [2:05]: hi there" in result


def test_get_transcript_json():
    segments = {
        "transcripts": [
            {
                "transcript": "hello",
                "speaker_id": 7,
                "start_offset": 0,
                "end_offset": 1000,
            }
        ]
    }
    stub_client([StubResponse(200, segments), StubResponse(200, {"speech": SPEECH})])

    result = server.otterai_get_transcript(
        server.GetTranscriptInput(speech_id="otid-abc", response_format="json")
    )

    payload = json.loads(result)
    assert payload["title"] == "Weekly sync"
    assert payload["segments"] == [
        {"speaker": "Alice", "start_offset": 0, "end_offset": 1000, "text": "hello"}
    ]


def test_search_recording_no_hits():
    stub_client([StubResponse(200, {"hits": []})])

    result = server.otterai_search_recording(
        server.SearchRecordingInput(query="budget", speech_id="otid-abc")
    )

    assert "No matches for 'budget'" in result


def test_list_folders_markdown():
    folders = {"folders": [{"id": 11, "folder_name": "Standups", "speech_count": 4}]}
    stub_client([StubResponse(200, folders)])

    result = server.otterai_list_folders(server.ListFoldersInput())

    assert "**Standups** (id 11)" in result
    assert "4 recordings" in result


def test_expired_session_resets_client():
    stub_client([StubResponse(401, {})])

    result = server.otterai_list_recordings(server.ListRecordingsInput())

    assert result.startswith("Error:")
    assert "expired" in result
    assert server._client is None


def test_get_user_markdown():
    user = {"user": {"name": "Dave", "email": "dave@example.com", "id": "user-1"}}
    stub_client([StubResponse(200, user)])

    result = server.otterai_get_current_user(server.GetUserInput())

    assert "**Name**: Dave" in result
    assert "**Email**: dave@example.com" in result
