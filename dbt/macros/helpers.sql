{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ custom_schema_name if custom_schema_name else target.schema }}
{%- endmacro %}

{# Upper-case, trim and collapse inner whitespace. #}
{% macro clean_text(expr) -%}
    upper(trim(regexp_replace({{ expr }}, '\s+', ' ', 'g')))
{%- endmacro %}

{% macro jstr(field) -%}
    nullif(trim(json_extract_string(payload, '$.{{ field }}')), '')
{%- endmacro %}

{% macro jbool(field) -%}
    cast(json_extract(payload, '$.{{ field }}') as boolean)
{%- endmacro %}

{% macro vehicle_key(make, model, model_year) -%}
    md5(concat_ws('|', {{ make }}, {{ model }}, cast({{ model_year }} as varchar)))
{%- endmacro %}

{# Incremental filter: only bronze rows ingested after the last processed one. #}
{% macro since_last_ingestion() -%}
    {% if is_incremental() %}
    where ingested_at > (select coalesce(max(ingested_at), timestamptz '1900-01-01') from {{ this }})
    {% endif %}
{%- endmacro %}
