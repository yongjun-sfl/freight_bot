# Project Structure

```
.
├── bot
│   ├── Dockerfile              # python:3.11-slim + poppler-utils, installs requirements.txt
│   ├── requirements.txt        # pinned runtime deps (see httpx note inside)
│   ├── main.py                 # entrypoint: pool setup, handler registration, long-polling
│   ├── config.py               # table and index name constants
│   ├── schema_ddl.py           # SINGLE SOURCE OF TRUTH for the database schema
│   ├── ai_engine.py            # Gemini text parsing + vision OCR, location cache
│   ├── handlers.py             # Telegram message/document/album handlers
│   └── state_machine.py        # trip-leg business and compliance logic
├── tests
│   ├── conftest.py             # MySQL fixtures, intent builders, leg helpers
│   ├── test_state_machine.py   # 35 test functions, real MySQL
│   └── test_ai_engine.py       # 10 test functions, monkeypatched, no network
├── docker-compose.yml          # mysql_db + telegram_bot
├── docker-compose.test.yml     # test overlay (named volume, shuttle_db_test)
├── pytest.ini
├── requirements-dev.txt        # pytest only, kept out of the runtime image
├── PROJECT_STRUCTURE.md
└── .memory.md
```

Not in version control: `.env` (gitignored), `mysql_data/` (local DB volume).

## Running the tests

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm tests
```

45 test functions, 58 cases after parametrisation, ~35s.
Writes to `shuttle_db_test`; the harness refuses to start
unless the database name ends in `_test`, so production data is never at risk.

## Things worth knowing

- **`bot/schema_ddl.py` is the only schema definition.** `main.py` applies it at
  startup and the tests apply it to a fresh database on every run. `init.sql` and
  `schema.sql` used to exist alongside it, had drifted apart, and were deleted.
  Do not reintroduce a second copy.
- **`python-telegram-bot` is held at 20.8.** It requires `httpx~=0.26`, while
  `google-genai` 2.x requires `httpx>=0.28.1`. The two are mutually exclusive, so
  `google-genai` is pinned to 1.2.0. Upgrading it means upgrading PTB to 22.x first.
- **MySQL cannot use a `/mnt/c` bind mount.** InnoDB will not initialise on a
  Windows drvfs path. `docker-compose.test.yml` overrides the volume for local runs;
  the production path is unchanged and set via `MYSQL_DATA_PATH`.
- **Do not run `telegram_bot` locally against the production token.** Telegram
  permits one `getUpdates` poller per token, so a second one will conflict with
  EC2 and can swallow live driver messages.
