with vehicles as (
    select make, model, model_year from {{ ref('silver_recalls') }}
    union
    select make, model, model_year from {{ ref('silver_complaints') }}
)

select
    {{ vehicle_key('make', 'model', 'model_year') }} as vehicle_key,
    make,
    model,
    model_year
from vehicles
