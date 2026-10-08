# LLM Analysis and Visualisation

This folder contains the model-assisted classification and the notebooks used
to analyse and visualise the ABM review corpus.

## Prompt engineering workflow

The topic-analysis pipeline uses staged prompts rather than asking the model
to make every decision at once:

1. **ABM relevance screening**: decide whether the title and abstract contain
   sufficient evidence of agent-based or individual-based modelling.
2. **Structured classification**: assign a controlled topic category and
   preserve a short evidence field. The model must use only the supplied title
   and abstract and must not infer unsupported methods, locations, or findings.
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

- `LLM_filter_and_topic.py`: staged screening and topic-analysis prompts.
- `deepseek.py`: DeepSeek classification workflow with checkpointing and JSON
  validation.
- `ABM_LLM_visualization_v9.ipynb`: topic, agent-type, LLM-adoption, and trend
  visualisations.
- `ABM_collaboration_network.ipynb`: co-authorship network analysis and plots.
