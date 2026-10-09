"""Unit tests never send traces: optional LangSmith tracing is forced off here, before .env is loaded
(load_dotenv does not override variables that are already set). tests/test_tracing.py turns it on with a stand-in."""
import os

os.environ["LANGSMITH_TRACING"] = "false"
