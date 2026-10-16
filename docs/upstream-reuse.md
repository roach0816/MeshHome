# Upstream reuse record

| Upstream | How it is used | Version / commit | License |
| --- | --- | --- | --- |
| [meshcore-dev/meshcore_py](https://github.com/meshcore-dev/meshcore_py) (`meshcore` on PyPI) | Runtime dependency for the companion protocol; wrapped by `backend/app/radio/meshcore_tcp.py` | `meshcore==2.3.14` | MIT (declared by package) |

**No source code has been copied from other projects in v0.1.** The plan suggested adapting parts
of `adradr/meshcore-webui`; v0.1 instead implements its own UI and adapter. If code is imported
later, record the repository URL, commit SHA, copied paths, modifications, and license here, and add
any required notices to `THIRD_PARTY_NOTICES.md`.

The adapter was written by reading the installed `meshcore` 2.3.14 source (`commands/*.py`,
`reader.py`, `events.py`). It deliberately uses `send_msg` (single attempt) rather than
`send_msg_with_retry`, and passes `auto_reconnect=False` so the app's supervisor is the only reconnect
mechanism.
