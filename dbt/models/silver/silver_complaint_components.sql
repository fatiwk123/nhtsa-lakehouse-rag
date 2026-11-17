{{ config(materialized='table') }}

-- One row per (complaint, vehicle, component group).
-- Components are comma-separated, but some names contain ", " (e.g. "SERVICE BRAKES, HYDRAULIC"):
-- protect those before splitting.
with exploded as (
    select
        complaint_vehicle_key,
        odi_number,
        make,
        model,
        model_year,
        unnest(string_split(replace(components_raw, ', ', '§'), ',')) as part
    from {{ ref('silver_complaints') }}
    where components_raw is not null
)

select distinct
    complaint_vehicle_key,
    odi_number,
    make,
    model,
    model_year,
    trim(replace(part, '§', ', ')) as component_group
from exploded
where trim(part) <> ''
