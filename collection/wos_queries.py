"""WoS search definitions for the revised ABM corpus."""
from __future__ import annotations
from dataclasses import dataclass

CORE_STEMS = [
    "agent-based", "agent based", "individual-based", "individual based",
    "agent-oriented", "agent oriented", "multiagent",
    "agent simulation*", "agent model*",
    "individual-level", "individual level",
]
RELATED_TERMS = [
    "social simulation", "microsimulation", "micro-simulation", "micro simulation",
    "population simulation", "population-based simulation", "activity-based",
    "activity based", "complex adaptive system", "opinion dynamics",
    "computational social science", "computational sociology", "artificial society",
    "artificial societies", "artificial life", "heterogeneous agent", "heterogeneous agents",
]
SOFTWARE_TERMS = [
    "netlogo", "repast", "mason", "gama", "matsim", "mesa", "anylogic",
    "flame", "cormas", "ascape", "starlogo", "epimodel", "agentpy",
    "agents.jl", "agentscript", "jade", "jason", "spade", "sarl", "simudyne",
]

ALL_KEYWORDS = CORE_STEMS + RELATED_TERMS + SOFTWARE_TERMS

@dataclass(frozen=True)
class Query:
    query_id: str
    label: str
    starter_query: str
    expanded_query: str
    stage: str

def _topic(qid: str, label: str, expr: str) -> Query:
    return Query(qid, label, f"TS=({expr})", f"TS=({expr})", "cross")

CORE_QUERY = Query("abm_keywords", "ABM keyword vocabulary",
                   "TS=(" + " OR ".join(f'\"{x}\"' for x in ALL_KEYWORDS) + ")",
                   "TS=(" + " OR ".join(f'\"{x}\"' for x in ALL_KEYWORDS) + ")", "core")

CROSS_QUERIES = []

def query_for_api(query: Query, api_type: str) -> str:
    return query.expanded_query if api_type == "expanded" else query.starter_query

def doc_type_filter(include: list[str]) -> str:
    values = " OR ".join(f'"{x}"' if " " in x else x for x in include)
    return f"DT=({values})"
