"""Two-stage WoS search plan: category-based core plus bounded topic queries."""
from __future__ import annotations
from dataclasses import dataclass

CORE_CATEGORIES = ("Regional & Urban Planning", "Urban Studies")

@dataclass(frozen=True)
class Query:
    query_id: str
    label: str
    starter_query: str
    expanded_query: str
    stage: str

def _topic(qid: str, label: str, expr: str) -> Query:
    return Query(qid, label, f"TS=({expr})", f"TS=({expr})", "cross")

CORE_QUERY = Query(
    "core_categories", "Regional & Urban Planning + Urban Studies categories",
    # Starter API does not implement WC/SU. Dry-run reports this capability gap;
    # this field-tag expression is for Expanded API accounts.
    'WC=("Regional & Urban Planning") OR WC=("Urban Studies")',
    'WC=("Regional & Urban Planning") OR WC=("Urban Studies")', "core",
)

CROSS_QUERIES = [
    _topic("planning_terms", "planning, land use, regional, spatial and rural planning",
           '"urban planning" OR "city planning" OR "town planning" OR "regional planning" OR "spatial planning" OR "territorial planning" OR "land use planning" OR "land-use planning" OR "rural planning" OR "village planning"'),
    _topic("urban_design_change", "urban design, development, renewal, governance, morphology and growth",
           '"urban design" OR "urban development" OR "urban redevelopment" OR "urban renewal" OR "urban regeneration" OR "urban governance" OR "urban morphology" OR "urban form" OR "urban growth" OR "urban expansion"'),
    _topic("urban_policy_future", "urban resilience, sustainability and smart cities",
           '"urban resilience" OR (resilience AND (urban OR city OR cities OR planning OR spatial)) OR "urban sustainability" OR "sustainable urban development" OR (sustainability AND (urban OR city OR cities OR planning OR spatial)) OR "smart city" OR "smart cities"'),
    _topic("transport_access", "transport, transit, mobility, walkability and accessibility",
           '"transit-oriented development" OR "transit oriented development" OR "transport planning" OR "transportation planning" OR "urban mobility" OR "city mobility" OR (TOD AND (urban OR city OR cities) AND (transport OR transit OR planning)) OR (walkability AND (urban OR city OR neighborhood OR neighbourhood OR spatial)) OR "urban accessibility" OR "city accessibility" OR (accessibility AND (urban OR city OR neighborhood OR neighbourhood OR spatial))'),
    _topic("public_green", "urban public space, green space and green infrastructure",
           '(("public space" OR "public spaces") AND (urban OR city OR cities OR neighborhood OR neighbourhood OR spatial)) OR "urban green space" OR "urban green spaces" OR ("green infrastructure" AND (urban OR city OR cities OR planning OR spatial)) OR (("green space" OR "green spaces") AND (urban OR city OR cities OR neighborhood OR neighbourhood OR spatial))'),
    _topic("housing_equity", "housing, community, neighborhood planning and spatial equity",
           '(housing AND (planning OR urban OR city OR neighborhood OR neighbourhood OR spatial)) OR "housing planning" OR "community planning" OR "neighborhood planning" OR "neighbourhood planning" OR "spatial equity" OR "urban spatial equity"'),
    _topic("urban_health", "urban health and healthy cities (health term context-bounded)",
           '"urban health" OR "healthy city" OR "healthy cities" OR (health AND (urban OR city OR cities OR planning))'),
    _topic("urban_perception_computing", "urban perception and urban computing",
           '"urban perception" OR "perception of urban" OR "urban computing"'),
]

def query_for_api(query: Query, api_type: str) -> str:
    return query.expanded_query if api_type == "expanded" else query.starter_query

def doc_type_filter(include: list[str]) -> str:
    values = " OR ".join(f'"{x}"' if " " in x else x for x in include)
    return f"DT=({values})"
