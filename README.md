# otterai-api

Unofficial Python API for [otter.ai](http://otter.ai)

## Contents

-   [Installation](#installation)
-   [Setup](#setup)
-   [MCP connector](#mcp-connector)
-   [APIs](#apis)
    -   [User](#user)
    -   [Speeches](#speeches)
    -   [Speakers](#speakers)
    -   [Folders](#folders)
    -   [Groups](#groups)
    -   [Notifications](#notifications)
-   [Exceptions](#exceptions)

## Installation

`pip install .`

or in a virtual environment

```bash
python3 -m venv env
source env/bin/activate
pip install .
```

## Setup

```python
from otterai import OtterAI
otter = OtterAI()
otter.login('USERNAME', 'PASSWORD')
```

### Session persistence

Unattended consumers should not log in on every run (repeated logins can
trip Otter's rate limiting). Save the session once and reuse it until it
expires:

```python
otter = OtterAI()
if not (otter.load_session('session.json') and otter.is_session_valid()):
    otter.login('USERNAME', 'PASSWORD')
    otter.save_session('session.json')
```

## MCP connector

The repository ships an [MCP](https://modelcontextprotocol.io) server
(`otterai_mcp`) so MCP clients such as Claude Code and Claude Desktop can
access your Otter recordings, summaries and transcripts directly.

The `mcp` SDK is a core dependency, so a normal install is all it takes:

```bash
pip install .
```

Credentials come from the environment (a `.env` file also works):

-   `OTTERAI_USERNAME` / `OTTERAI_PASSWORD` — your Otter.ai login
-   `OTTERAI_SESSION_FILE` — optional; where the login session is cached
    (default `~/.otterai/session.json`). The server reuses a saved session
    across runs and only logs in when it has expired, which avoids
    tripping Otter's login rate limiting.

Register with Claude Code:

```bash
claude mcp add otterai \
    -e OTTERAI_USERNAME=you@example.com \
    -e OTTERAI_PASSWORD=... \
    -- otterai-mcp
```

or add it to Claude Desktop's `claude_desktop_config.json`:

```json
{
    "mcpServers": {
        "otterai": {
            "command": "otterai-mcp",
            "env": {
                "OTTERAI_USERNAME": "you@example.com",
                "OTTERAI_PASSWORD": "..."
            }
        }
    }
}
```

(`python -m otterai_mcp` is equivalent to the `otterai-mcp` entry point.)

Tools exposed:

| Tool                        | Description                                             |
| --------------------------- | ------------------------------------------------------- |
| `otterai_get_current_user`  | Account info / auth check                               |
| `otterai_list_recordings`   | List recordings with ids, dates and summary snippets    |
| `otterai_get_recording`     | Full metadata, speakers and summary of one recording    |
| `otterai_get_summary`       | Just the summary of one recording                       |
| `otterai_get_transcript`    | Full transcript with speaker names and timestamps       |
| `otterai_search_recording`  | Search for text within one recording                    |
| `otterai_list_folders`      | List folders (ids usable in `otterai_list_recordings`)  |
| `otterai_export_recording`  | Download a recording export (txt/pdf/mp3/docx/srt/zip)  |

## APIs

### User

Get user specific data

```python
otter.get_user()
```

### Speeches

Get all speeches

**optional parameters**: folder, page_size, source

```python
otter.get_speeches()
```

Get speech by id

```python
otter.get_speech(SPEECH_ID)
```

Get the full transcript of a speech (the segments behind "View in Otter")

```python
otter.get_speech_transcript(SPEECH_ID)
```

Query a speech

```python
otter.query_speech(QUERY, SPEECH_ID)
```

Upload a speech

**optional parameters**: content_type (default audio/mp4)

```python
otter.upload_speech(FILE_NAME)
```

Download a speech

**optional parameters**: filename (defualt id), format (default: all available (txt,pdf,mp3,docx,srt) as zip file)

```python
otter.download_speech(SPEECH_ID, FILE_NAME)
```

Move a speech to trash

```python
otter.move_to_trash_bin(SPEECH_ID)
```

#### TODO

Start a live speech

### Speakers

Get all speakers

```python
otter.get_speakers()
```

Create a speaker

```python
otter.create_speaker(SPEAKER_NAME)
```

#### TODO

Assign a speaker to speech transcript

### Folders

Get all folders

```python
otter.get_folders()
```

### Groups

Get all groups

```python
otter.list_groups()
```

### Notifications

Get notification settings

```python
otter.get_notification_settings()
```

## Exceptions

```python
from otterai import OtterAIException

try:
 ...
except OtterAIException as e:
 ...
```
