# WoS DOI corpus enrichment

Run from the repository root with `OPENALEX_API_KEY` set:

```powershell
python -m process.enrich_wos_openalex
```

Alternatively, `python collection/openalex/run_wos_enrichment.py` reuses the
existing DOI-stage credential from `process/enrich.py` if the environment
variable is absent. Its value is loaded in memory and never printed or copied
into a new configuration file.

The first run retains every record with a nonempty DOI in the main WoS Parquet/JSONL.
The original 700,005-record corpus is archived under
`database/backups/before_doi_filter_20261004/`.
`database/wos_urban_planning_doi.parquet` is the immutable DOI-only WoS input.

Exact DOI requests contain up to 100 identifiers. Eight workers share one
8-request/second limiter. Retries count toward a conservative 10,000-request
project budget. Account budget exhaustion pauses the run without purchasing
additional credits. API responses are stored as individual gzip JSON files in
`database/raw/openalex/wos_doi/`. A SQLite queue and per-DOI patches are committed
after each batch. Checkpoint and usage files are under `database/`.

Restarting the same command resumes pending DOIs and reuses saved batch responses.
Use `--export-only` to rebuild outputs from the saved metadata without API calls.
Final and periodic exports stream the DOI-only input and fill empty canonical
fields. OpenAlex citation counts, topics, extracted keywords, language and
authorship data are also stored in separate `openalex_*` columns. All original
WoS fields remain recoverable from the archived corpus; `wos_original_metadata`
stores the original values of fields filled on each row.

OpenAlex institution countries are author affiliations, not study locations.
OpenAlex topics are distinct from WoS categories and later study annotations.
Author keywords are not filled with algorithmic OpenAlex keywords. No LLM is
called. Unmatched and invalid DOIs remain in the corpus with explicit statuses.

Main outputs are `database/wos_urban_planning.parquet` and `.jsonl`.
Progress: `database/wos_openalex_checkpoint.json`.
Statistics: `database/wos_openalex_enrichment_summary.json`.
The existing CMD progress window automatically displays the enrichment stage.
