# Paperless-GPT retry guards

The document counters `OCR_MAX_RETRIES` and `AUTO_TAG_MAX_RETRIES` are set to 1; 0 disables their limit. In paperless-gpt v0.31.0, the metadata client treats `LLM_MAX_RETRIES` values of 0 or less as the default of 3. Setting it to 1 permits at most two provider attempts per metadata operation: the initial attempt and one retry. The separate vision client accepts `VISION_LLM_MAX_RETRIES=0` to disable its provider retries.

These controls reduce retries; they do not enforce a hard $5 spending cap. Failed tag removal or restarts can lead to additional attempts. Before paid recovery, independently provision and verify an enforced budget. For the OpenAI metadata provider in v0.31.0, `LLM_MAX_TOKENS` is ignored and is therefore not set here.

Documents longer than `OCR_LIMIT_PAGES` must be explicitly held for metadata-only processing, with existing OCR preserved. Compose cannot enforce this per-document policy by itself; operators must enforce it during recovery.
