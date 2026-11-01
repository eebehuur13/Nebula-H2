# Nebula Core

Nebula Core lets Claude search the Google Drive files people in an organisation share with it, and shows each person only the files Drive lets them open. It's an MCP server (MCP is the protocol Claude uses to talk to connectors) that each person adds to their own Claude as one custom connector. A small web front end shows people and admins what's there.

We built it at Rocket Learning (RL). It's a trimmed-down version of [Nebula V1](https://github.com/eebehuur13/Nebula-V1) that keeps only the Drive search path, so it's easier to scale and to experiment with how search ranks documents.

## What it does

Someone at RL shares a Drive folder or a single file with Nebula's Google service account (a Google account that belongs to Nebula, not to a person). Nebula notices the share within a few minutes, downloads each file, turns it into text and tables, and makes it searchable. After that, anyone who can open a file in Drive can find and read it through Claude, and nobody else can.

A Shared Drive needs one more step. After adding Nebula's account as a member, someone at RL connects it by pasting its link to Claude, because Drive doesn't say who added Nebula to a Shared Drive. Nebula ignores anything shared with it from accounts outside RL.

Nebula keeps the text it read and the spreadsheet tables, but not a copy of each file. When Claude asks to see a page or a picture, Nebula downloads the file from Drive at that moment.

Claude sees six tools:

| Tool | What Claude uses it for |
|---|---|
| `search` | Find passages about a question, across every file the person can open |
| `documents` | List the files the person can open, a page at a time |
| `open` | Read one file in full, or one page, slide or sheet of it, or see a page as a picture |
| `query` | Answer number questions with SQL over spreadsheets that Nebula loaded as tables, run in a sandbox that holds only that person's tables |
| `sources` | Add a Drive link now, refresh a file, check how far an import has got, list what the person has shared with Nebula, or stop reading a folder |
| `job` | Check on a refresh or import that's still running |

If you change a tool, update its description in `src/nebula/server.py` too. claude.ai ignores a connector's general instructions, so those descriptions are the only guidance Claude gets about when and how to use each tool.

## Who can see what

Drive's own sharing decides who can see each file. Each time Nebula reads a file, it also reads the file's sharing list and stores it in the `item_access` table:
- If Drive shares a file with one person, only that person finds it in Nebula. If Drive shares it with five people, those five find it.
- If Drive shares it with everyone in your organisation's domain (`NEBULA_ALLOWED_EMAIL_DOMAINS`), everyone who signs in with an account in that domain finds it.
- If Drive shares it with a Google Group, the group's members don't find it yet. Nebula can't see who's in a group, so it only records the group's address. Members can still find the file if Drive also shares it with them by email or with the RL domain.
- If Drive shares it with "anyone with the link", Nebula ignores that, because a link isn't a person.
- If Nebula couldn't read a file's sharing list (Drive only shows it to editors, so this happens when Nebula was added as a Viewer), only admins can see the file.

Admins can open everything. Someone is an admin if they're named in the `NEBULA_ADMIN_EMAILS` setting, or if they were made one with `nebula admin`. Removing them from the setting, or running `nebula admin --remove`, takes it away. The person who shared a folder with Nebula can refresh or disconnect it, but they only find the files inside it that Drive also shares with them.

Changes in Drive reach Nebula on the next check. That's every two minutes by default, and an admin can change the timing on the admin page.

## How number questions work

Spreadsheets laid out as tables of data are loaded into Postgres, one table per sheet, in a schema called `data`. Claude's SQL never runs there, though. For each `query`, Nebula copies only the tables the query names, and only if the person can open the files they came from, into a fresh in-memory DuckDB (a small SQL engine that runs inside Nebula's Python process). File and network access are switched off and the settings are locked before the query runs, so the query can't reach anything else. Each query gets 15 seconds and up to 2,000 rows. DuckDB's SQL is close to PostgreSQL's, with a few differences, such as `strftime` instead of `to_char`. `tests/test_access.py` holds the ways people found to escape the old guard, as a regression test.

## How a search works

Search lives in `src/nebula/knowledge/search.py`. This is the part to change when trying other ranking methods. A search goes through six steps:

1. **Scope.** It works out which files the person can open.
2. **Embed.** It turns the question into a vector, which is a list of 1,024 numbers that captures its meaning, using the same model that embedded the files.
3. **Meaning.** It finds the 40 passages whose vectors are closest to the question's.
4. **Words.** It also runs a plain keyword search over all passages, using Postgres's full-text search with common English and Hindi words removed, and takes the top 40 from that.
5. **Fuse, then rerank.** The two lists are combined with reciprocal rank fusion. Each passage scores 1/(60 + its rank) in each list it appears in, so a passage near the top of both lists comes first. If a reranker is switched on, it then reads the question with each of the top 30 passages (their first 2,000 characters) and scores how well each passage answers the question. The reranker is a second model that compares a passage to the question directly, which is slower and more accurate than comparing vectors. On a CPU-only test server we reranked only the top 12 passages and their first 800 characters, to save CPU time (`NEBULA_SEARCH_RERANK_CANDIDATES` and `NEBULA_SEARCH_RERANK_CHARS`).
6. **Assemble.** Passages are grouped by file, keeping at most two excerpts per file. A file whose text is 4,000 characters or less comes back whole, so Claude doesn't answer about it from two excerpts. Each search returns at most 12,000 characters of whole files.

Every search Claude makes is recorded with each step's results and timings. An admin can open any search from the admin page's Activity tab ("See every step") and see what each step found and how the reranker reordered it. That record is the quickest way to see why a document did or didn't come back.

To measure a ranking change, run the eval harness (`src/nebula/evaluate.py`). Each case is a question, who asks it, and what a good answer needs: which file should come back, and which words should be in it. The harness reports, for each case, where the answer went missing: in the extracted text, in the search results, or in the excerpt Claude would see. `ops/eval/cases.rl.json` holds 38 questions written on 20 October 2026 from four real RL documents, including the 30 June AP WCD meeting minutes. Those documents have to be imported before the cases can pass.

```bash
uv run nebula eval ops/eval/my-cases.json --out results.json
```

## The models

Two models do the work in `src/nebula/models.py`. The embedding model turns text into vectors, and the reranker scores passages against a question. Each can run locally on the CPU or on Amazon Bedrock:

| Setting | Local, on the CPU | Bedrock |
|---|---|---|
| `NEBULA_EMBED_PROVIDER` | `local`: multilingual-e5-large | `bedrock-cohere`: Cohere Embed v4, or `bedrock-titan`: Titan Text Embeddings V2 (English-optimised) |
| `NEBULA_RERANK_PROVIDER` | `local`: bge-reranker-v2-m3 | `bedrock-cohere`: Cohere Rerank 3.5 |

The embedding setting defaults to `local`. The reranker setting defaults to `none`, which skips reranking and keeps the fused order from step 5, so set it to `local` or `bedrock-cohere` to rerank. Every stored vector records which model made it, so switching the embedding model means embedding every file again.

The two local models take about 2.8 GB on disk, and every server process loads its own copy into memory. On an 8-vCPU, 32 GB test server, eight API processes together peaked at about 20 GB of memory. Moving the models to Bedrock takes that load off the servers. The Bedrock paths are covered by tests with stand-in AWS clients, but we haven't run them against the real service yet.

`python -m nebula.bench` measures how many passages a minute the local embedding model handles at a given number of CPU threads.

## What's where

| Path | What it is |
|---|---|
| `src/nebula/server.py` | The six MCP tools and their descriptions |
| `src/nebula/access.py` | Sign-in identity and who can open which file |
| `src/nebula/knowledge/` | Files (`items.py`), the search index (`index.py`), search (`search.py`), spreadsheet tables and the `query` sandbox (`datasets.py`), the document list (`inventory.py`), and where each file is in being read (`stages.py`) |
| `src/nebula/ingest/` | Turning files (Docs, Sheets, Slides, PDF, Word, PowerPoint, Excel, CSV, text) into structured text and tables, splitting them into passages, and noting which parts were pictures or scans that extraction missed |
| `src/nebula/connectors/gdrive.py` | Reading Drive: what changed, downloading files, and reading each file's sharing list |
| `src/nebula/sync.py` | The background side of Drive: finding new shares, syncing changes, refreshing one file, and downloading an original when Claude asks for a picture |
| `src/nebula/sources.py` | What the `sources` tool does and what it tells Claude |
| `src/nebula/jobs.py`, `worker.py` | The job queue in Postgres and the background worker that runs it |
| `src/nebula/webui.py`, `me.py`, `admin.py`, `admin_ui/` | Google sign-in for the browser, the file list at `/web`, "My files" at `/me`, and the admin page at `/admin` |
| `src/nebula/migrations/001_init.sql` | The whole database schema |
| `infra/` | An AWS CDK stack (Fargate, Aurora, CloudFront) for deploying Nebula on AWS. Nebula V1 ran on an earlier version of it; this version hasn't been deployed yet. See `infra/README.md`. |

## Compared with Nebula V1

Nebula Core doesn't have:
- the Notion and Granola connectors
- teams, leads, roles and groups, or handing someone's work over to a successor
- sharing inside Nebula (the `share` tool), so Drive's sharing is the only kind
- wiki pages, records and the links between them (the `save` tool)
- shared skills
- file uploads and downloads through Claude's sandbox (the `file` tool) and the signed links behind them
- per-person storage quotas and the daily limit on writes
- gap-tool plugins, the admin MCP tool and the Drive probe
- Kruti Dev conversion, which turned Hindi typed in that old font into Unicode
- stored copies of every Drive file, and the S3 bucket and storage code behind them

Saving what Claude read from a picture (previously `save` with kind `transcription`) was removed along with the `save` tool. Claude can still look at a page as a picture with `open`.

The database schema is one file instead of seven migrations.

## Run it locally

Nebula needs Postgres with the pgvector extension. These commands run it in Docker on port 5433, with a database called `nebula` for running Nebula.

```bash
docker run -d --name nebula-pg -e POSTGRES_PASSWORD=nebula -e POSTGRES_USER=nebula \
  -e POSTGRES_DB=nebula -p 5433:5432 pgvector/pgvector:pg17
uv sync
uv run nebula serve            # http://localhost:8000/mcp, dev sign-in (no Google)
uv run nebula worker           # in another terminal
```

Every setting is in `src/nebula/config.py` and can be overridden with a `NEBULA_*` environment variable or a `.env` file. Dev mode signs everyone in as `NEBULA_DEV_USER_EMAIL`, so the web app refuses to start in dev mode unless `NEBULA_PUBLIC_URL` is localhost. Locally the app logs a warning that its database user is a superuser, which is fine on a laptop.

Other commands:

```bash
uv run nebula admin you@example.org      # make someone an admin
uv run nebula admin them@example.org --remove   # stop them being one
uv run nebula discover                          # look for new Drive shares now
uv run nebula run-jobs                          # run queued jobs until none are left
```

## Run it on a server

On a server, Nebula refuses to start until sign-in and its database user are set up safely, and it needs one more setting to read Drive:
- **Sign-in.** Set `NEBULA_AUTH_MODE=google`, `NEBULA_ENV=prod`, and `NEBULA_JWT_SIGNING_KEY` to a random secret of at least 32 characters. That key signs every sign-in, so the built-in placeholder would let anyone sign in as anyone.
- **The database user.** Nebula shouldn't log in to Postgres as a superuser. An ordinary user can't install pgvector, though, so a superuser runs `create extension vector` once in Nebula's database. After that, Nebula runs as an ordinary user that owns the database, and it creates its own tables. On Aurora, the master user isn't a true Postgres superuser, so it passes the check.
- **Drive.** Set `NEBULA_GOOGLE_SERVICE_ACCOUNT_JSON` to Nebula's Google identity: a workload federation config, or a service-account key.

Also set your organisation's details: `NEBULA_ALLOWED_EMAIL_DOMAINS` (your Google Workspace domain, which decides who can sign in and who counts as part of the organisation), `NEBULA_ADMIN_EMAILS`, `NEBULA_PUBLIC_URL`, and optionally `NEBULA_ABOUT_URL`, `NEBULA_PRIVACY_URL` and `NEBULA_TERMS_URL` for the footer links on the web pages.

### Running more than one of each

You can run several API processes (`nebula serve --workers N`) and several background workers (`nebula worker`, once per machine or container). Each worker renews a lease on the job it's working on every 10 seconds. A job whose lease hasn't been renewed for 90 seconds belongs to a worker that stopped, so another worker picks it up. A worker that loses a job this way doesn't overwrite the other worker's result.

Every process keeps two database connection pools: Nebula's own (`NEBULA_DB_POOL_SIZE`, 10 by default) and a small one for sign-in data (`NEBULA_DB_SIGNIN_POOL_SIZE`, 2 by default). With 8 API processes and 2 workers, that's up to 120 connections. If that's more than the database allows, put PgBouncer or RDS Proxy in front of it in transaction mode, and set `NEBULA_DB_POOLER=true`. That setting turns off the prepared statements a transaction-mode pooler can't carry between connections.

A daily clean-up job deletes finished jobs after 14 days (`NEBULA_JOBS_KEEP_DAYS`), and requests, search traces, sign-ins and indexing events after 90 days (`NEBULA_ACTIVITY_KEEP_DAYS`). The admin page can only show as far back as that.

## Tests

```bash
docker exec nebula-pg psql -U nebula -c "create database nebula_test"   # once
uv run pytest -q
```

The tests use fake embeddings and their own database, and 116 of them pass. `tests/test_core.py` runs the product through the MCP tools. `tests/test_access.py` covers who can see what (including the `query` sandbox), `tests/test_safety.py` covers the startup checks and other safety limits, `tests/test_scale.py` covers job leases, the clean-up and file reading off the main loop, `tests/test_drive_space.py` covers discovery and sync against a fake Drive, and `tests/test_connectors.py` covers the Drive connector against mocked Drive API responses.
