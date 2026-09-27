"""Callback module referenced by examples/litellm/config.yaml.

The proxy imports ``proxy_handler_instance`` at start-up. Configuration comes from the
environment: ``AIECON_WORKSPACE`` (raw JSONL lands under ``<workspace>/raw/YYYY-MM-DD/``),
``AIECON_DATASET_ID`` and ``AIECON_SCOPE_ID``.
"""

from aiecon.collect.litellm_callback import AieconLiteLLMLogger

proxy_handler_instance = AieconLiteLLMLogger.from_env()
