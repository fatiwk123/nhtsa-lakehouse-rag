{{ config(materialized='incremental', unique_key='recall_key', incremental_strategy='delete+insert') }}

with src as (
    select * from {{ source('bronze', 'nhtsa_recalls') }}
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
        trim(v.record_id)                                         as campaign_number,
        v.make_norm                                               as make,
        coalesce(mo.canonical_model, v.model_clean)               as model,
        v.model_year,
        {{ jstr('NHTSAActionNumber') }}                           as action_number,
        {{ jstr('Manufacturer') }}                                as manufacturer,
        {{ clean_text(jstr('Component')) }}                       as component,
        trim(split_part({{ clean_text(jstr('Component')) }}, ':', 1)) as component_group,
        cast(try_strptime({{ jstr('ReportReceivedDate') }}, '%d/%m/%Y') as date) as report_received_date,
        {{ jstr('Summary') }}                                     as summary,
        {{ jstr('Consequence') }}                                 as consequence,
        {{ jstr('Remedy') }}                                      as remedy,
        {{ jstr('Notes') }}                                       as notes,
        {{ jbool('parkIt') }}                                     as park_it,
        {{ jbool('parkOutSide') }}                                as park_outside,
        {{ jbool('overTheAirUpdate') }}                           as over_the_air_update,
        v.row_hash,
        v.ingestion_id,
        v.ingested_at
    from vehicle v
    left join {{ ref('model_aliases') }} mo
        on mo.make = v.make_norm and mo.alias = v.model_clean
)

select
    md5(concat_ws('|', campaign_number, make, model, cast(model_year as varchar))) as recall_key,
    *
from parsed
where campaign_number is not null and campaign_number <> ''
qualify row_number() over (partition by recall_key order by ingested_at desc) = 1
