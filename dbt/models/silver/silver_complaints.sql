{{ config(materialized='incremental', unique_key='complaint_vehicle_key', incremental_strategy='delete+insert') }}

with src as (
    select * from {{ source('bronze', 'nhtsa_complaints') }}
    {{ since_last_ingestion() }}
),

vehicle as (
    select
        src.*,
        coalesce(ma.canonical_make, {{ clean_text('src.make') }}) as make_norm,
        {{ clean_text('src.model') }} as model_clean
    from src
    left join {{ ref('make_aliases') }} ma on {{ clean_text('src.make') }} = ma.alias
),

parsed as (
    select
        trim(v.record_id)                                   as odi_number,
        v.make_norm                                         as make,
        coalesce(mo.canonical_model, v.model_clean)         as model,
        v.model_year,
        {{ jstr('manufacturer') }}                          as manufacturer,
        coalesce({{ jbool('crash') }}, false)               as crash,
        coalesce({{ jbool('fire') }}, false)                as fire,
        coalesce(cast(json_extract(payload, '$.numberOfInjuries') as integer), 0) as number_of_injuries,
        coalesce(cast(json_extract(payload, '$.numberOfDeaths') as integer), 0)   as number_of_deaths,
        -- 12/31/1969 is the API's placeholder for "unknown" (epoch 0).
        nullif(cast(try_strptime({{ jstr('dateOfIncident') }}, '%m/%d/%Y') as date), date '1969-12-31')     as incident_date,
        nullif(cast(try_strptime({{ jstr('dateComplaintFiled') }}, '%m/%d/%Y') as date), date '1969-12-31') as complaint_filed_date,
        {{ jstr('vin') }}                                   as vin_prefix,
        {{ clean_text(jstr('components')) }}                as components_raw,
        {{ jstr('summary') }}                               as summary,
        v.row_hash,
        v.ingestion_id,
        v.ingested_at
    from vehicle v
    left join {{ ref('model_aliases') }} mo
        on mo.make = v.make_norm and mo.alias = v.model_clean
)

select
    md5(concat_ws('|', odi_number, make, model, cast(model_year as varchar))) as complaint_vehicle_key,
    *
from parsed
where odi_number is not null and odi_number <> ''
qualify row_number() over (partition by complaint_vehicle_key order by ingested_at desc) = 1
