-- Nebula schema.
-- People who sign in, the Google Drive source and what Nebula read from it (files, spreadsheet
-- tables, the search index), who may open each file, the work queue, and what the admin page shows.

create extension if not exists vector;

-- People ---------------------------------------------------------------------------

create table people (
    id            uuid primary key default gen_random_uuid(),
    email         text not null unique,
    name          text not null default '',
    google_sub    text unique,
    status        text not null default 'active' check (status in ('active', 'deactivated')),
    is_admin      boolean not null default false,
    created_at    timestamptz not null default now(),
    last_seen_at  timestamptz
);

-- The source -----------------------------------------------------------------------

-- Everything people share with Nebula's Google service account lands in one Drive collection,
-- keyed by Drive file id, so a file is stored and indexed once however many people or folders
-- bring it in.
create table collections (
    id                 uuid primary key default gen_random_uuid(),
    slug               text not null unique check (slug ~ '^[a-z0-9-]{2,60}$'),
    name               text not null,
    description        text not null default '',
    kind               text not null check (kind in ('gdrive')),
    source_config      jsonb not null default '{}',
    status             text not null default 'active' check (status in ('active', 'paused', 'archived')),
    sync_cursor        text,
    last_synced_at     timestamptz,
    last_full_sync_at  timestamptz,
    last_acl_sweep_at  timestamptz,   -- when every file's sharing was last re-read in full
    sync_error         text,
    created_at         timestamptz not null default now()
);

insert into collections (slug, name, description, kind)
values ('drive', 'Google Drive',
        'Folders, files and Shared Drives people share with Nebula. Each file is visible only to the people Drive shares it with.',
        'gdrive');

-- Each shared folder, file or Shared Drive is a root; whoever shared it manages it.
create table source_roots (
    id             uuid primary key default gen_random_uuid(),
    collection_id  uuid not null references collections (id) on delete cascade,
    external_id    text not null,
    kind           text not null check (kind in ('folder', 'file', 'drive')),
    name           text not null default '',
    managed_by     uuid references people (id),
    shared_by      text not null default '',          -- the sharer as Drive reports it
    added_via      text not null default 'discovery' check (added_via in ('discovery', 'request')),
    status         text not null default 'active' check (status in ('active', 'removed', 'lost')),
    status_note    text not null default '',
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    unique (collection_id, external_id)
);
create index source_roots_manager on source_roots (managed_by);

-- Files ----------------------------------------------------------------------------

create table items (
    id                  uuid primary key default gen_random_uuid(),
    collection_id       uuid not null references collections (id) on delete cascade,
    external_id         text not null,
    kind                text not null,
    title               text not null default '',
    url                 text not null default '',
    mime_type           text not null default '',
    filename            text not null default '',
    path                text[] not null default '{}',
    author              text not null default '',
    source_created_at   timestamptz,
    source_modified_at  timestamptz,
    source_root         text,          -- the root that brought the file in
    source_rev          text,          -- Drive's revision marker, so unchanged files aren't downloaded again
    acl_fingerprint     text,          -- a fingerprint of its sharing, so unchanged sharing isn't re-read
    source_audience     jsonb,         -- who Drive shares it with, as last seen
    seen_at             timestamptz not null default now(),  -- last listed by Drive; a full listing deletes the rest
    pieces              int not null default 0,  -- search passages (and table cards), kept by the indexer so
    pieces_waiting      int not null default 0,  -- pages listing many files don't count chunks on every refresh
    content_hash        text,
    version             int not null default 0,
    document            jsonb,
    -- active: read and searchable; error: Nebula couldn't read it (status_note says why); deleted: gone
    -- from Drive or from what's shared with Nebula, kept as a marker so it's read again if it comes back.
    status              text not null default 'active' check (status in ('active', 'error', 'deleted')),
    status_note         text not null default '',
    indexed_at          timestamptz,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    unique (collection_id, external_id)
);
create index items_collection on items (collection_id, status);
create index items_source_root on items (collection_id, source_root) where source_root is not null;

-- Who may open each file, mirrored from Drive on every sync: 'email:<address>' for a person, or
-- 'domain:<domain>' for a whole Workspace domain Nebula serves. Admins can open everything; a
-- file whose sharing Nebula couldn't read has no rows, so only admins see it.
create table item_access (
    item_id    uuid not null references items (id) on delete cascade,
    principal  text not null,
    primary key (item_id, principal)
);
create index item_access_principal on item_access (principal);

-- Spreadsheets loaded as real tables (in schema "data"); each follows its file's access. The `query`
-- tool copies only the tables a person may open into a sandboxed DuckDB; it never runs SQL here.
create schema if not exists data;

create table datasets (
    id             uuid primary key default gen_random_uuid(),
    collection_id  uuid not null references collections (id) on delete cascade,
    item_id        uuid not null references items (id) on delete cascade,
    sheet          text not null default '',
    table_name     text not null unique,
    title          text not null,
    card           text not null default '',
    columns        jsonb not null default '[]',
    row_count      int not null default 0,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    unique (item_id, sheet)
);

-- Search index ---------------------------------------------------------------------

-- Passages of a file's text, and the card of each spreadsheet table. Every chunk names the item
-- it came from, which decides who can find it.
create table chunks (
    id           bigserial primary key,
    item_id      uuid not null references items (id) on delete cascade,
    source_type  text not null check (source_type in ('item', 'dataset')),
    source_id    uuid not null,
    seq          int not null default 0,
    kind         text not null default 'text',
    heading      text not null default '',
    text         text not null,
    locator      jsonb not null default '{}',
    embedding    halfvec(1024),
    embed_model  text,
    tsv          tsvector generated always as (to_tsvector('simple', heading || ' ' || text)) stored,
    created_at   timestamptz not null default now()
);
create index chunks_source on chunks (source_type, source_id);
create index chunks_item on chunks (item_id);
create index chunks_tsv on chunks using gin (tsv);
create index chunks_embedding on chunks using hnsw (embedding halfvec_cosine_ops);
create index chunks_unembedded on chunks (id) where embedding is null;

-- Jobs: the work queue and anything long-running -------------------------------------

create table jobs (
    id            uuid primary key default gen_random_uuid(),
    kind          text not null,
    payload       jsonb not null default '{}',
    status        text not null default 'queued' check (status in ('queued', 'running', 'done', 'failed', 'cancelled')),
    priority      int not null default 100,
    run_after     timestamptz not null default now(),
    attempts      int not null default 0,
    max_attempts  int not null default 5,
    dedupe_key    text,
    person_id     uuid references people (id),
    progress      real,
    message       text not null default '',
    result        jsonb,
    error         text,
    locked_by     text,
    locked_at     timestamptz,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
create unique index jobs_dedupe on jobs (dedupe_key) where status in ('queued', 'running');
create index jobs_ready on jobs (priority, run_after) where status = 'queued';
create index jobs_running on jobs (updated_at) where status = 'running';
create index jobs_finished on jobs (kind, updated_at) where status in ('done', 'failed', 'cancelled');

-- Requests per person per minute, shared by every API process.
create table usage_counters (
    person_id     uuid not null references people (id),
    metric        text not null,
    window_start  timestamptz not null,
    used          bigint not null default 0 check (used >= 0),
    primary key (person_id, metric, window_start)
);
create index usage_counters_expiry on usage_counters (window_start);

-- What the admin page shows ------------------------------------------------------------

-- Sign-ins, sources added and removed, and admin-page actions.
create table audit_log (
    id             bigserial primary key,
    at             timestamptz not null default now(),
    person_id      uuid references people (id),
    action         text not null,
    resource_type  text,
    resource_id    text,
    detail         jsonb not null default '{}'
);
create index audit_resource on audit_log (resource_type, resource_id);

-- One row per tool call Claude makes on someone's behalf. For a search, `results` is what came
-- back in order and `trace` is every step with its timing, so admins can see why.
create table tool_calls (
    id            bigserial primary key,
    at            timestamptz not null default now(),
    person_id     uuid references people (id) on delete set null,
    tool          text not null,
    action        text not null default '',
    summary       text not null default '',     -- the question, title or link, in a line
    target        text not null default '',     -- the id or link it acted on, when there is one
    args          jsonb not null default '{}',  -- the arguments, with long text cut down
    ok            boolean not null,
    error         text not null default '',
    result_count  int,
    ms            int not null default 0,
    client        text not null default '',     -- the calling app's user agent, cut down
    results       jsonb,
    trace         jsonb
);
create index tool_calls_at on tool_calls (at desc);
create index tool_calls_person_at on tool_calls (person_id, at desc);

-- Each app a person has connected Nebula to (claude.ai, Claude Desktop, ...).
create table mcp_clients (
    person_id      uuid not null references people (id) on delete cascade,
    client         text not null,
    first_seen_at  timestamptz not null default now(),
    last_seen_at   timestamptz not null default now(),
    connects       int not null default 1,
    primary key (person_id, client)
);

-- What indexing did to each file: read, removed, failed, made searchable.
create table index_events (
    id             bigserial primary key,
    at             timestamptz not null default now(),
    item_id        uuid references items (id) on delete set null,
    title          text not null default '',
    collection_id  uuid references collections (id) on delete set null,
    event          text not null check (event in ('read', 'removed', 'failed', 'searchable')),
    detail         text not null default '',
    ms             int,
    job_id         uuid
);
create index index_events_at on index_events (at desc);

-- Small runtime settings (refresh timings) and the worker's heartbeat.
create table app_settings (
    key         text primary key,
    value       jsonb not null,
    updated_at  timestamptz not null default now(),
    updated_by  uuid references people (id) on delete set null
);
