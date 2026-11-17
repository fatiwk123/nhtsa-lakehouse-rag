with groups as (
    select distinct component_group from {{ ref('silver_recalls') }} where component_group is not null
    union
    select distinct component_group from {{ ref('silver_complaint_components') }}
),

matched as (
    select
        g.component_group,
        c.category,
        c.category_fr,
        row_number() over (partition by g.component_group order by c.priority nulls last) as rn
    from groups g
    left join {{ ref('component_categories') }} c on g.component_group like c.pattern
)

select
    md5(component_group)              as component_key,
    component_group,
    coalesce(category, 'OTHER')       as category,
    coalesce(category_fr, 'Autre')    as category_fr
from matched
where rn = 1
