# Chub Chat Exporter

Mass-export your own Chub chats to JSONL. You can also save each character card as a PNG.

## Requirements

- Windows 10 or 11
- Python 3.10 or newer
- Google Chrome

## First Setup

1. Double-click `install.bat`.
2. Double-click `run_exporter.bat`.
3. Pick `Login / setup Chub profile` and sign in.
4. Run `Export all chats` or `Export all chats + chatted with bot's`.

## Output

- Chats save to `chat_exports/`.
- Chat files are named `CharacterName-chat-000.jsonl`, `CharacterName-chat-001.jsonl`, etc.
- Bot PNG mode saves character cards as `CharacterName.png`.
- `export_manifest.csv`, `chat_links.txt`, `failed_exports.csv`, and the browser profile are created on your machine after you run it.

## Notes

- If signing in opens but nothing loads, close the CMD window, finish logging in inside Chrome, then run `run_exporter.bat` again and use an export mode.
- If you want to rerun a category, delete the matching CSV/output files first.
- If you see any bugs, please let me know.
- The first export on the run will be slow because it has to open and load everything for the first time. After that, exports are usually much faster. You can watch the CMD window or the export files to see progress.
