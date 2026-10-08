# LLM Analysis and Visualisation

This folder contains the model-assisted classification and the notebooks used
to analyse and visualise the ABM review corpus.

## Prompt engineering workflow

The topic-analysis pipeline uses staged prompts rather than asking the model
to make every decision at once:

1. **ABM relevance screening**: decide whether the title and abstract contain
   sufficient evidence of agent-based or individual-based modelling.
2. **Structured classification**: assign a controlled topic category and
   identify the dominant agent category and standardized `agent_type`, and
   determine whether a large language model is actually used in the research.
   The output also records the primary LLM role. Mere mentions of GPT, LLMs,
   or generative AI do not count as use.
3. **Fine-grained topic analysis**: assign standardized secondary topics and a
   small set of informative keywords only for records that pass screening.

The prompts use a clear role, explicit evidence boundaries, closed label sets,
normalization rules, and a strict JSON schema. Responses containing Markdown
fences or extra prose are cleaned before parsing, while parse failures are
recorded per record rather than silently treated as valid classifications.

For reproducibility, preserve the model name, prompt text, sampling settings,
input title/abstract, raw response, checkpoint, and error field. API keys must
be supplied through environment variables; no credentials belong in this
repository.

## Files

- `deepseek.py`: DeepSeek ABM/topic classification, LLM-use detection, role
  classification, checkpointing, and JSON validation.
- `ABM_LLM_visualization_v9.ipynb`: topic, agent-type, LLM-adoption, and trend
  visualisations.
- `ABM_collaboration_network.ipynb`: co-authorship network analysis and plots.
