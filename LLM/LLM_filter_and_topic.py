import json
import os

os.environ["VLLM_USE_FLASHINFER_SAMPLER"] = "0"
import re
from datetime import datetime
from typing import Dict, List, Any
from vllm import LLM, SamplingParams

# Paths configuration
INPUT_PATH = os.environ.get("ABM_INPUT_JSONL", "<INPUT_JSONL>")
MODEL_PATH = os.environ.get("LLM_MODEL_PATH", "<MODEL_PATH>")
OUTPUT_DIR = os.environ.get("LLM_OUTPUT_DIR", "<OUTPUT_DIR>")
OUTPUT_PATH = os.environ.get("ABM_OUTPUT_JSONL", "<OUTPUT_JSONL>")
CHECKPOINT_PATH = os.environ.get("ABM_CHECKPOINT_JSONL", "<CHECKPOINT_JSONL>")

LLAMA_FIELDS = [
    "llama_abm_is_abm",
    "llama_abm_evidence",
    "llama_abm_triggered_keywords",
    "llama_abm_methodology",
    "llama_topic_category",
    "llama_abm_confidence",
    "llama_abm_error",
    "llama_llm_has_llm",
    "llama_llm_evidence",
    "llama_llm_role",
    "llama_llm_confidence",
    "llama_llm_error",
    "llama_primary_domain",
    "llama_secondary_topics",
    "llama_agent_types",
    "llama_simulation_scale",
    "llama_topic_keywords",
    "llama_topic_error",
    "llama_stage_status",
    "llama_model",
    "llama_processed_at",
]

# Standardized Taxonomy Instructions
ABM_SYSTEM_PROMPT = r"""You are an expert literature screening assistant specializing in Agent-Based Modeling (ABM) and Multi-Agent Systems (MAS).

Task: Analyze the title and abstract to screen ABM papers and extract normalized search trigger words.

Rules for `triggered_keywords`:
1. Extract 1-5 candidate agent-related terms that caused paper retrieval.
2. NORMALIZE and STANDARDIZE terms into standard canonical forms (e.g., convert "multiagent systems", "multi agent framework" -> "Multi-Agent System"; "swarm intelligence", "swarming" -> "Swarm Robotics / Intelligence"; "reinforcement learning agents" -> "Reinforcement Learning Agent").
3. Avoid generic terms like "system", "model", "paper", or "method".

Rules for `topic_category`:
Must select from: ["Urban & Transportation", "Epidemiology & Public Health", "Economics & Financial Markets", "Social Dynamics & Opinion", "Ecology & Environmental Resource", "Robotics & Autonomous Swarms", "Computer Networks & Distributed Systems", "Other / General"]

Return ONLY valid JSON:
{
  "is_abm": 0 or 1,
  "triggered_keywords": ["Canonical Term 1", "Canonical Term 2"],
  "abm_evidence": "1-2 concise sentences in English",
  "abm_methodology": "Standardized method (e.g., Cellular Automata, Individual-Based Model, Multi-Agent Simulation, Spatial ABM, None)",
  "topic_category": "Selected standard topic category or None",
  "confidence": 0.0 to 1.0
}"""

LLM_SYSTEM_PROMPT = r"""You are an expert literature screening assistant.

Task: Determine if Large Language Models (LLMs) or Foundation Models are integrated into the research simulation or modeling framework.

Rules for `llm_role`:
Must map the functional role to ONE of these standardized categories:
- "Agent Decision Engine" (LLM controls agent cognitive/reasoning behavior)
- "Natural Language Interface" (LLM acts as translation/user interface)
- "Data Generation / Augmentation" (LLM generates synthetic agents/profiles)
- "Code / Model Generation" (LLM writes code/scripts for the model)
- "Content / Sentiment Analysis" (LLM parses text external to simulation)
- "None" (No LLM used)

Return ONLY valid JSON:
{
  "has_llm": 0 or 1,
  "llm_evidence": "1-2 concise sentences in English",
  "llm_role": "Standardized category string",
  "confidence": 0.0 to 1.0
}"""

TOPIC_SYSTEM_PROMPT = r"""You are an expert computational social science research analyst.

Task: Extract structured, granular domain details from an ABM paper. Avoid over-broad generalizations or overly specific entity names.

Rules:
1. primary_domain: Pick ONE from ["Urban Transportation & Mobility", "Epidemiological Spread", "Financial Market Microstructure", "Resource Management & Sustainability", "Social Opinion Dynamics", "Policy & Disaster Response", "Swarm Robotics & Control", "Distributed Computing & MAS"].
2. secondary_topics: Provide 2-3 standardized sub-topics (e.g., ["EV Charging Infrastructure", "Commuter Route Choice"]).
3. agent_types: Standardize entity types into categories like "Individuals / Citizens", "Vehicles / Vessels", "Firms / Financial Entities", "Robots / UAVs", "Biological / Pathogen Units", "Government / Institutional Bodies".
4. simulation_scale: Pick ONE from ["Micro / Individual-level", "Meso / Neighborhood-level", "Macro / Population-level", "Network-level", "Unspecified"].
5. topic_keywords: 3-5 high-value normalized keywords.

Return ONLY valid JSON:
{
  "primary_domain": "Standardized domain string",
  "secondary_topics": ["Subtopic 1", "Subtopic 2"],
  "agent_types": "Standardized agent entity description",
  "simulation_scale": "Standardized scale string",
  "topic_keywords": ["keyword1", "keyword2", "keyword3"]
}"""


def clean_json_output(raw_text: str) -> Dict[str, Any]:
    text = raw_text.strip()
    text = re.sub(r"^```json\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^```\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    return json.loads(text.strip())


def load_processed_ids(checkpoint_path: str) -> set:
    processed_ids = set()
    if os.path.exists(checkpoint_path):
        with open(checkpoint_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        item = json.loads(line)
                        if "doc_id" in item:
                            processed_ids.add(item["doc_id"])
                    except Exception:
                        continue
    return processed_ids


def build_input_prompt(doc: Dict[str, Any], system_prompt: str) -> str:
    title = doc.get("title", "")
    abstract = doc.get("abstract", "")
    user_content = f"Title: {title}\nAbstract: {abstract}"

    # Standard ChatML / Llama-3 instruction format
    return f"<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n\n{system_prompt}<|eot_id|><|start_header_id|>user<|end_header_id|>\n\n{user_content}<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n"


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    processed_ids = load_processed_ids(CHECKPOINT_PATH)

    raw_docs = []
    with open(INPUT_PATH, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                doc = json.loads(line)
                if doc.get("doc_id") not in processed_ids:
                    raw_docs.append(doc)

    if not raw_docs:
        print("All documents have already been processed.")
        return

    # Initialize vLLM engine optimizing for 320GB VRAM
    llm = LLM(
        model=MODEL_PATH,
        tensor_parallel_size=4,
        max_model_len=4096,
        gpu_memory_utilization=0.90,
        trust_remote_code=True
    )

    sampling_params = SamplingParams(
        temperature=0.1,
        top_p=0.95,
        max_tokens=512
    )

    print(f"Loaded {len(raw_docs)} un processed documents. Starting execution...")

    # Stage 1: Screening & Trigger Words
    stage1_prompts = [build_input_prompt(doc, ABM_SYSTEM_PROMPT) for doc in raw_docs]
    stage1_outputs = llm.generate(stage1_prompts, sampling_params)

    abm_docs = []
    abm_indices = []

    stage1_results = []
    for idx, (doc, output) in enumerate(zip(raw_docs, stage1_outputs)):
        raw_text = output.outputs[0].text
        res = {
            "llama_abm_is_abm": 0,
            "llama_abm_evidence": "",
            "llama_abm_triggered_keywords": [],
            "llama_abm_methodology": "none",
            "llama_topic_category": "none",
            "llama_abm_confidence": 0.0,
            "llama_abm_error": None
        }
        try:
            parsed = clean_json_output(raw_text)
            res["llama_abm_is_abm"] = int(parsed.get("is_abm", 0))
            res["llama_abm_evidence"] = str(parsed.get("abm_evidence", ""))
            res["llama_abm_triggered_keywords"] = parsed.get("triggered_keywords", [])
            res["llama_abm_methodology"] = str(parsed.get("abm_methodology", "none"))
            res["llama_topic_category"] = str(parsed.get("topic_category", "none"))
            res["llama_abm_confidence"] = float(parsed.get("confidence", 0.0))
        except Exception as e:
            res["llama_abm_error"] = str(e)

        stage1_results.append(res)
        if res["llama_abm_is_abm"] == 1:
            abm_docs.append(doc)
            abm_indices.append(idx)

    # Stage 2: LLM Integration Detection (Only for ABM papers)
    stage2_results = {}
    if abm_docs:
        stage2_prompts = [build_input_prompt(doc, LLM_SYSTEM_PROMPT) for doc in abm_docs]
        stage2_outputs = llm.generate(stage2_prompts, sampling_params)
        for orig_idx, output in zip(abm_indices, stage2_outputs):
            raw_text = output.outputs[0].text
            res = {
                "llama_llm_has_llm": 0,
                "llama_llm_evidence": "",
                "llama_llm_role": "none",
                "llama_llm_confidence": 0.0,
                "llama_llm_error": None
            }
            try:
                parsed = clean_json_output(raw_text)
                res["llama_llm_has_llm"] = int(parsed.get("has_llm", 0))
                res["llama_llm_evidence"] = str(parsed.get("llm_evidence", ""))
                res["llama_llm_role"] = str(parsed.get("llm_role", "none"))
                res["llama_llm_confidence"] = float(parsed.get("confidence", 0.0))
            except Exception as e:
                res["llama_llm_error"] = str(e)
            stage2_results[orig_idx] = res

    # Stage 3: Fine-Grained Topic Analysis (Only for ABM papers)
    stage3_results = {}
    if abm_docs:
        stage3_prompts = [build_input_prompt(doc, TOPIC_SYSTEM_PROMPT) for doc in abm_docs]
        stage3_outputs = llm.generate(stage3_prompts, sampling_params)
        for orig_idx, output in zip(abm_indices, stage3_outputs):
            raw_text = output.outputs[0].text
            res = {
                "llama_primary_domain": "Unspecified",
                "llama_secondary_topics": [],
                "llama_agent_types": "Unspecified",
                "llama_simulation_scale": "Unspecified",
                "llama_topic_keywords": [],
                "llama_topic_error": None
            }
            try:
                parsed = clean_json_output(raw_text)
                res["llama_primary_domain"] = str(parsed.get("primary_domain", "Unspecified"))
                res["llama_secondary_topics"] = parsed.get("secondary_topics", [])
                res["llama_agent_types"] = str(parsed.get("agent_types", "Unspecified"))
                res["llama_simulation_scale"] = str(parsed.get("simulation_scale", "Unspecified"))
                res["llama_topic_keywords"] = parsed.get("topic_keywords", [])
            except Exception as e:
                res["llama_topic_error"] = str(e)
            stage3_results[orig_idx] = res

    # Assemble Final Jsonl Output
    processed_time = datetime.now().isoformat()
    with open(CHECKPOINT_PATH, "a", encoding="utf-8") as f_out:
        for idx, doc in enumerate(raw_docs):
            s1 = stage1_results[idx]
            s2 = stage2_results.get(idx, {
                "llama_llm_has_llm": 0,
                "llama_llm_evidence": "",
                "llama_llm_role": "none",
                "llama_llm_confidence": 0.0,
                "llama_llm_error": None
            })
            s3 = stage3_results.get(idx, {
                "llama_primary_domain": "none",
                "llama_secondary_topics": [],
                "llama_agent_types": "none",
                "llama_simulation_scale": "none",
                "llama_topic_keywords": [],
                "llama_topic_error": None
            })

            combined_record = dict(doc)

            # Map values to requested LLAMA_FIELDS schema
            combined_record["llama_abm_is_abm"] = s1["llama_abm_is_abm"]
            combined_record["llama_abm_evidence"] = s1["llama_abm_evidence"]
            combined_record["llama_abm_triggered_keywords"] = s1["llama_abm_triggered_keywords"]
            combined_record["llama_abm_methodology"] = s1["llama_abm_methodology"]
            combined_record["llama_topic_category"] = s1["llama_topic_category"]
            combined_record["llama_abm_confidence"] = s1["llama_abm_confidence"]
            combined_record["llama_abm_error"] = s1["llama_abm_error"]

            combined_record["llama_llm_has_llm"] = s2["llama_llm_has_llm"]
            combined_record["llama_llm_evidence"] = s2["llama_llm_evidence"]
            combined_record["llama_llm_role"] = s2["llama_llm_role"]
            combined_record["llama_llm_confidence"] = s2["llama_llm_confidence"]
            combined_record["llama_llm_error"] = s2["llama_llm_error"]

            combined_record["llama_primary_domain"] = s3["llama_primary_domain"]
            combined_record["llama_secondary_topics"] = s3["llama_secondary_topics"]
            combined_record["llama_agent_types"] = s3["llama_agent_types"]
            combined_record["llama_simulation_scale"] = s3["llama_simulation_scale"]
            combined_record["llama_topic_keywords"] = s3["llama_topic_keywords"]
            combined_record["llama_topic_error"] = s3["llama_topic_error"]

            combined_record["llama_stage_status"] = "complete"
            combined_record["llama_model"] = MODEL_PATH.split("/")[-1]
            combined_record["llama_processed_at"] = processed_time

            f_out.write(json.dumps(combined_record, ensure_ascii=False) + "\n")

    # Sync checkpoint file to final output path
    os.system(f"cp {CHECKPOINT_PATH} {OUTPUT_PATH}")
    print("Task completed successfully. Output updated.")


if __name__ == "__main__":
    main()
