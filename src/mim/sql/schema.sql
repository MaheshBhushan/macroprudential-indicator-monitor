-- Core storage: series catalog, ingest runs, raw pull log, vintaged observations, quality flags.
create schema if not exists mim;

create table if not exists mim.series (
    series_id      text primary key,
    source         text not null check (source in ('BIS', 'ECB', 'ESTAT')),
    dataflow       text not null,
    series_key     text not null,
    country        text not null,
    indicator      text not null,
    unit           text not null,
    expected_freq  text not null check (expected_freq in ('A', 'Q', 'M')),
    description    text not null
);

create table if not exists mim.ingest_run (
    run_id         serial primary key,
    mode           text not null check (mode in ('live', 'fixture')),
    as_of          date not null,
    started_at     timestamptz not null default now(),
    finished_at    timestamptz,
    series_ok      int,
    series_failed  int,
    obs_inserted   int,
    obs_revised    int,
    obs_withdrawn  int
);

create table if not exists mim.series_pull (
    run_id              int not null references mim.ingest_run,
    series_id           text not null references mim.series,
    url                 text not null,
    http_status         int,
    ok                  boolean not null,
    n_observations      int,
    content_sha256      text,
    source_last_update  text,
    error               text,
    primary key (run_id, series_id)
);

-- One row per (series, period, vintage). A vintage is open while valid_to_run is null; when a
-- later run sees a different value/status (revision) or no longer sees the period (withdrawal)
-- the open row is closed with that run's id. Observations as of run R are the rows with
-- valid_from_run <= R and (valid_to_run is null or valid_to_run > R).
create table if not exists mim.observation_vintage (
    series_id       text not null references mim.series,
    period          text not null,
    period_start    date not null,
    value           numeric,
    obs_status      text,
    valid_from_run  int not null references mim.ingest_run,
    valid_to_run    int references mim.ingest_run,
    primary key (series_id, period, valid_from_run),
    check (valid_to_run is null or valid_to_run > valid_from_run)
);
create unique index if not exists observation_vintage_open
    on mim.observation_vintage (series_id, period) where valid_to_run is null;

create table if not exists mim.quality_flag (
    flag_id    bigserial primary key,
    run_id     int not null references mim.ingest_run,
    series_id  text not null references mim.series,
    period     text,
    flag       text not null,
    severity   text not null check (severity in ('info', 'warning', 'error')),
    detail     text not null
);
create index if not exists quality_flag_run on mim.quality_flag (run_id, series_id);

create unlogged table if not exists mim.stg_observation (
    series_id     text not null,
    period        text not null,
    period_start  date not null,
    value         numeric,
    obs_status    text
);

-- Set-based merge of the staged pull into the vintage table. Only series present in
-- series_pull with ok = true for this run take part, so a failed fetch never withdraws data.
create or replace function mim.merge_staging(p_run_id int)
returns table (inserted int, revised int, withdrawn int)
language plpgsql as $$
declare
    v_ins int;
    v_rev int;
    v_wd  int;
begin
    with changed as (
        update mim.observation_vintage v
           set valid_to_run = p_run_id
          from mim.stg_observation s
         where v.series_id = s.series_id
           and v.period = s.period
           and v.valid_to_run is null
           and (v.value is distinct from s.value or v.obs_status is distinct from s.obs_status)
        returning v.series_id, v.period, v.value as old_value, s.value as new_value,
                  v.obs_status as old_status, s.obs_status as new_status
    )
    insert into mim.quality_flag (run_id, series_id, period, flag, severity, detail)
    select p_run_id, series_id, period, 'revised', 'info',
           format('value %s -> %s; status %s -> %s',
                  coalesce(old_value::text, 'NULL'), coalesce(new_value::text, 'NULL'),
                  coalesce(old_status, 'NULL'), coalesce(new_status, 'NULL'))
      from changed;
    get diagnostics v_rev = row_count;

    with gone as (
        update mim.observation_vintage v
           set valid_to_run = p_run_id
         where v.valid_to_run is null
           and v.series_id in (select series_id from mim.series_pull
                                where run_id = p_run_id and ok)
           and not exists (select 1 from mim.stg_observation s
                            where s.series_id = v.series_id and s.period = v.period)
        returning v.series_id, v.period, v.value
    )
    insert into mim.quality_flag (run_id, series_id, period, flag, severity, detail)
    select p_run_id, series_id, period, 'withdrawn', 'warning',
           format('period no longer published; last value %s', coalesce(value::text, 'NULL'))
      from gone;
    get diagnostics v_wd = row_count;

    insert into mim.observation_vintage
        (series_id, period, period_start, value, obs_status, valid_from_run)
    select s.series_id, s.period, s.period_start, s.value, s.obs_status, p_run_id
      from mim.stg_observation s
     where not exists (select 1 from mim.observation_vintage v
                        where v.series_id = s.series_id and v.period = s.period
                          and v.valid_to_run is null);
    get diagnostics v_ins = row_count;

    return query select v_ins, v_rev, v_wd;
end;
$$;

-- Point-in-time view of the dataset exactly as it stood after a given run.
create or replace function mim.observations_as_of(p_run_id int)
returns table (series_id text, period text, period_start date, value numeric, obs_status text,
               valid_from_run int)
language sql stable as $$
    select series_id, period, period_start, value, obs_status, valid_from_run
      from mim.observation_vintage
     where valid_from_run <= p_run_id
       and (valid_to_run is null or valid_to_run > p_run_id)
$$;
