select
    cc.complaint_vehicle_key,
    cc.odi_number,
    {{ vehicle_key('cc.make', 'cc.model', 'cc.model_year') }} as vehicle_key,
    c.component_key,
    cc.component_group,
    c.category,
    c.category_fr
from {{ ref('silver_complaint_components') }} cc
join {{ ref('dim_component') }} c on c.component_group = cc.component_group
