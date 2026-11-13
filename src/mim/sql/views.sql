-- Indicator and quality views. Re-runnable: every object is dropped and recreated.
drop view if exists mim.v_latest_snapshot, mim.v_indicator_panel, mim.v_series_quality,
    mim.v_bank_loan_growth, mim.v_government_debt, mim.v_total_credit, mim.v_debt_service,
    mim.v_property_price_growth, mim.v_credit_gap, mim.v_revisions, mim.observation_current
    cascade;

create view mim.observation_current as
select v.series_id, s.source, s.country, s.indicator, s.unit, s.expected_freq,
       v.period, v.period_start, v.value, v.obs_status, v.valid_from_run
  from mim.observation_vintage v
  join mim.series s using (series_id)
 where v.valid_to_run is null;

-- Every value change between consecutive vintages of the same observation.
create view mim.v_revisions as
select n.series_id, s.country, s.indicator, n.period, n.period_start,
       o.value as previous_value, n.value as revised_value,
       n.value - o.value as revision,
       o.obs_status as previous_status, n.obs_status as revised_status,
       o.valid_from_run as previous_run, n.valid_from_run as revised_in_run,
       r.as_of as revised_as_of
  from mim.observation_vintage n
  join mim.observation_vintage o
    on o.series_id = n.series_id and o.period = n.period and o.valid_to_run = n.valid_from_run
  join mim.series s on s.series_id = n.series_id
  join mim.ingest_run r on r.run_id = n.valid_from_run;

-- Basel III countercyclical buffer guide: add-on starts at a 2 pp gap and reaches 2.5% at 10 pp.
create view mim.v_credit_gap as
select country, period, period_start, value as credit_gap_pp,
       case when value is null then null
            when value < 2 then 'below_2pp'
            when value < 10 then '2_to_10pp'
            else 'above_10pp' end as ccyb_band,
       round(least(greatest((value - 2) / 8 * 2.5, 0), 2.5), 2) as indicative_ccyb_pct
  from mim.observation_current
 where indicator = 'credit_gap';

create view mim.v_property_price_growth as
with p as (
    select country, period, period_start,
           max(value) filter (where indicator = 'rpp_real_index') as real_index,
           max(value) filter (where indicator = 'rpp_nominal_index') as nominal_index
      from mim.observation_current
     where indicator in ('rpp_real_index', 'rpp_nominal_index')
     group by country, period, period_start
)
select c.country, c.period, c.period_start, c.real_index, c.nominal_index,
       round(100 * (c.real_index / nullif(y.real_index, 0) - 1), 2) as real_yoy_pct,
       round(100 * (c.nominal_index / nullif(y.nominal_index, 0) - 1), 2) as nominal_yoy_pct
  from p c
  left join p y
    on y.country = c.country and y.period_start = (c.period_start - interval '1 year')::date;

-- BIS publishes DSR levels; the deviation from each series' own full-sample mean is the
-- early-warning reading used in BIS work on debt service burdens.
create view mim.v_debt_service as
select country, period, period_start,
       case indicator when 'dsr_private' then 'private_nonfinancial'
                      when 'dsr_households' then 'households'
                      when 'dsr_nfc' then 'nonfinancial_corporations' end as sector,
       value as dsr_pct,
       round(avg(value) over (partition by series_id), 2) as long_run_avg_pct,
       round(value - avg(value) over (partition by series_id), 2) as deviation_pp
  from mim.observation_current
 where indicator in ('dsr_private', 'dsr_households', 'dsr_nfc');

create view mim.v_total_credit as
select c.country, c.period, c.period_start, c.value as credit_gdp_pct,
       c.value - y.value as change_4q_pp
  from mim.observation_current c
  left join mim.observation_current y
    on y.series_id = c.series_id and y.period_start = (c.period_start - interval '1 year')::date
 where c.indicator = 'total_credit_gdp';

create view mim.v_government_debt as
select c.country, c.period, c.period_start, c.value as gov_debt_gdp_pct,
       c.value - y.value as change_4q_pp, c.obs_status
  from mim.observation_current c
  left join mim.observation_current y
    on y.series_id = c.series_id and y.period_start = (c.period_start - interval '1 year')::date
 where c.indicator = 'gov_debt_gdp';

create view mim.v_bank_loan_growth as
select country, period, period_start,
       case indicator when 'hh_loan_growth' then 'households'
                      else 'nonfinancial_corporations' end as borrower,
       round(value, 2) as loan_growth_yoy_pct
  from mim.observation_current
 where indicator in ('hh_loan_growth', 'nfc_loan_growth');

-- Quality summary per series, based on the latest run that attempted it.
create view mim.v_series_quality as
with last_pull as (
    select distinct on (series_id) *
      from mim.series_pull
     order by series_id, run_id desc
), cur as (
    select series_id, count(*) as n_observations,
           count(*) filter (where value is null) as n_missing_values,
           min(period_start) as first_period_start, max(period_start) as last_period_start
      from mim.observation_current
     group by series_id
), fl as (
    select f.series_id, f.run_id,
           count(*) filter (where f.flag = 'gap_in_series') as n_gap_periods,
           count(*) filter (where f.flag = 'revised') as n_revised,
           string_agg(distinct f.flag, ',' order by f.flag) as flags
      from mim.quality_flag f
     group by f.series_id, f.run_id
)
select s.series_id, s.source, s.country, s.indicator, s.expected_freq,
       lp.run_id as last_run, lp.http_status, lp.ok as last_pull_ok,
       coalesce(cur.n_observations, 0) as n_observations,
       coalesce(cur.n_missing_values, 0) as n_missing_values,
       coalesce(fl.n_gap_periods, 0) as n_gap_periods,
       coalesce(fl.n_revised, 0) as n_revised_last_run,
       cur.first_period_start, cur.last_period_start,
       coalesce(fl.flags, '') as flags_last_run
  from mim.series s
  left join last_pull lp on lp.series_id = s.series_id
  left join cur on cur.series_id = s.series_id
  left join fl on fl.series_id = s.series_id and fl.run_id = lp.run_id;

-- Long-format panel for Tableau: one row per series and expected period between the first and
-- last published period, so absent periods appear as rows with is_missing_period = true.
create view mim.v_indicator_panel as
with bounds as (
    select series_id, min(period_start) as lo, max(period_start) as hi
      from mim.observation_current
     group by series_id
), calendar as (
    select s.series_id, s.source, s.country, s.indicator, s.unit, s.expected_freq,
           g::date as period_start
      from mim.series s
      join bounds b using (series_id)
     cross join lateral generate_series(
           b.lo, b.hi,
           case s.expected_freq when 'M' then interval '1 month'
                                when 'Q' then interval '3 months'
                                else interval '1 year' end) g
), last_run as (
    select series_id, max(run_id) as run_id from mim.series_pull where ok group by series_id
)
select c.series_id, c.source, c.country, c.indicator, c.unit, c.expected_freq,
       c.period_start, o.period, o.value, o.obs_status,
       o.series_id is null as is_missing_period,
       o.series_id is not null and o.value is null as is_missing_value,
       exists (select 1 from mim.quality_flag f
                where f.series_id = c.series_id and f.run_id = lr.run_id
                  and f.period = o.period and f.flag = 'revised') as is_revised_last_run,
       exists (select 1 from mim.quality_flag f
                where f.series_id = c.series_id and f.run_id = lr.run_id
                  and f.flag = 'stale_series') as series_stale,
       exists (select 1 from mim.quality_flag f
                where f.series_id = c.series_id and f.run_id = lr.run_id
                  and f.flag in ('frequency_mismatch', 'mixed_frequency')) as frequency_issue
  from calendar c
  left join last_run lr on lr.series_id = c.series_id
  left join mim.observation_current o
    on o.series_id = c.series_id and o.period_start = c.period_start;

-- Latest available reading per country for the headline indicators.
create view mim.v_latest_snapshot as
with latest as (
    select distinct on (country, indicator) country, indicator, period, value
      from mim.observation_current
     where value is not null
       and indicator in ('credit_gap', 'total_credit_gdp', 'dsr_private', 'gov_debt_gdp')
     order by country, indicator, period_start desc
), rpp as (
    select distinct on (country) country, period, real_yoy_pct
      from mim.v_property_price_growth
     where real_yoy_pct is not null
     order by country, period_start desc
)
select c.country,
       max(l.period) filter (where l.indicator = 'credit_gap') as credit_gap_period,
       max(l.value) filter (where l.indicator = 'credit_gap') as credit_gap_pp,
       max(l.value) filter (where l.indicator = 'total_credit_gdp') as credit_gdp_pct,
       max(l.value) filter (where l.indicator = 'dsr_private') as dsr_private_pct,
       max(l.value) filter (where l.indicator = 'gov_debt_gdp') as gov_debt_gdp_pct,
       max(rpp.period) as rpp_period,
       max(rpp.real_yoy_pct) as rpp_real_yoy_pct
  from (select distinct country from mim.series) c
  left join latest l on l.country = c.country
  left join rpp on rpp.country = c.country
 group by c.country
having count(l.country) > 0 or count(rpp.country) > 0;
