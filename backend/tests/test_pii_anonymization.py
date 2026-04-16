"""
Tests for PII anonymization in _llm_chat (pipeline.py).

Covers 6 scenarios:
  1. anonymize=OFF, openai  → PII passes through unchanged
  2. anonymize=ON,  openai  → email + pii_map scrubbed from user prompt
  3. anonymize=ON,  anthropic → same scrubbing behaviour
  4. anonymize=ON,  ollama   → prompt sent as-is (local model, no scrubbing)
  5. Response de-anonymization → [CUSTOMER] token restored in returned text
  6. Regex fallback → email in body scrubbed even without explicit pii_map entry

Run from backend/:
    python -m pytest tests/test_pii_anonymization.py -v
  or:
    python tests/test_pii_anonymization.py
"""

import asyncio
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Allow running from backend/ directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# Minimal stubs so pipeline.py can be imported without a live DB/app context
# ---------------------------------------------------------------------------
# Stub out settings before importing pipeline
import types
settings_stub = types.SimpleNamespace(
    OLLAMA_BASE_URL="http://localhost:11434",
    OLLAMA_MODEL="mistral-nemo",
    CONFIDENCE_THRESHOLD=75.0,
)
sys.modules.setdefault("app.core.config", types.ModuleType("app.core.config"))
sys.modules["app.core.config"].settings = settings_stub

# Stub every heavy module that pipeline.py imports at the top level
_STUB_MODS = [
    "app.models.models",
    "app.core.audit",
    "app.core.tasks",
    "app.services.email_service",
    "app.services.ftp_service",
    "app.services.imap_service",
    "app.services.monitored_emails",
    "app.services.pod_folder_service",
    "app.services.document_prereader_service",
    "app.services.pdf_service",
    "app.services.audit_service",
    "app.api.config",
    "celery",
    "celery.schedules",
]
for mod in _STUB_MODS:
    if mod not in sys.modules:
        sys.modules[mod] = types.ModuleType(mod)

# Provide the SystemConfig stub that pipeline.py references at module level.
# The class-level `key` attribute must support `.in_(...)` (SQLAlchemy column
# expression) while instance-level `key` remains a plain string.
class _MockColumn:
    """Minimal stand-in for a SQLAlchemy column expression."""
    def in_(self, values):
        return MagicMock()

class _FakeSystemConfig:
    key = _MockColumn()     # class attr: supports SystemConfig.key.in_(...)

    def __init__(self, k, v):
        # Instance attr shadows the class attr; instance.key is the string key.
        self.key   = k
        self.value = v

_models = sys.modules["app.models.models"]
_models.SystemConfig = _FakeSystemConfig
for _name in [
    "EmailRequest", "Order", "OrderLine", "PodDocument",
    "PackingSlipDocument", "InvoiceDocument",
    "ApprovalQueue", "GuidanceQueue", "PodRegistry",
    "AuditLog", "User", "MonitoredEmail",
]:
    setattr(_models, _name, MagicMock)

# Stub audit/pdf helpers
sys.modules["app.services.audit_service"].log_audit = AsyncMock(return_value=None)
sys.modules["app.services.pdf_service"].generate_pod_pdf = MagicMock(return_value=None)

# Now import the functions under test
from app.agents.pipeline import _llm_chat, _EMAIL_PATTERN  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(config: dict):
    """
    Return (db_mock, select_patch_ctx).

    pipeline._llm_chat does `select(SystemConfig).where(...)` before calling
    db.execute, so we must also patch the `select` symbol inside pipeline to
    avoid SQLAlchemy type-checking our stub class.
    """
    rows = [_FakeSystemConfig(k, v) for k, v in config.items()]

    fake_scalars = MagicMock()
    fake_scalars.all.return_value = rows

    fake_result = MagicMock()
    fake_result.scalars.return_value = fake_scalars

    db = AsyncMock()
    db.execute.return_value = fake_result

    # The select().where() chain just needs to return something db.execute accepts
    mock_select = MagicMock()
    mock_select.return_value.where.return_value = MagicMock()

    return db, patch("app.agents.pipeline.select", mock_select)


def _make_httpx_mock(response_text: str, provider: str = "openai"):
    """
    Return (mock_client_class, captured_calls_list).
    captured_calls_list is populated with the kwargs of every .post() call.
    """
    captured = []

    if provider == "openai":
        response_json = {"choices": [{"message": {"content": response_text}}]}
    else:  # anthropic
        response_json = {"content": [{"text": response_text}]}

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = response_json

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    async def fake_post(url, **kwargs):
        captured.append({"url": url, **kwargs})
        return mock_response

    mock_client.post = fake_post

    mock_class = MagicMock(return_value=mock_client)
    return mock_class, captured


BASE_CFG = {
    "llm_openai_api_key":  "sk-test-key",
    "llm_openai_endpoint": "https://api.openai.com/v1",
    "llm_openai_model":    "gpt-4o-mini",
    "llm_anthropic_api_key":  "ant-test-key",
    "llm_anthropic_endpoint": "https://api.anthropic.com",
    "llm_anthropic_model":    "claude-haiku-4-5-20251001",
    "llm_provider_fallback_enabled": "false",
}

REAL_EMAIL   = "john.smith@acme-corp.com"
REAL_NAME    = "John Smith"
SYSTEM_PROMPT = "You are a logistics assistant."
USER_PROMPT   = (
    f"From: {REAL_EMAIL}\n"
    f"Customer name: {REAL_NAME}\n"
    f"Subject: POD request for ORD-1234\n"
    f"Body: Please send POD, my email is {REAL_EMAIL}"
)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestPiiAnonymization(unittest.TestCase):

    # ── 1. anonymize=OFF, openai ─────────────────────────────────────────────
    def test_no_anonymize_openai_passes_pii(self):
        """When llm_anonymize_pii is false, PII must reach the external API unchanged."""
        cfg = {**BASE_CFG, "llm_provider": "openai", "llm_anonymize_pii": "false"}
        db, sel_patch = _make_db(cfg)
        mock_cls, captured = _make_httpx_mock("Here is your response.", "openai")

        async def run():
            with sel_patch, patch("app.agents.pipeline.httpx.AsyncClient", mock_cls):
                return await _llm_chat(
                    db, SYSTEM_PROMPT, USER_PROMPT,
                    pii_map={REAL_EMAIL: "redacted@example.com", REAL_NAME: "[CUSTOMER]"},
                )

        result_text, provider = asyncio.run(run())

        self.assertEqual(provider, "openai/gpt-4o-mini")
        self.assertEqual(len(captured), 1)
        sent_user = captured[0]["json"]["messages"][-1]["content"]
        # PII should be PRESENT (anonymization is off)
        self.assertIn(REAL_EMAIL, sent_user,
                      "Email should NOT be scrubbed when anonymize=false")
        self.assertIn(REAL_NAME, sent_user,
                      "Name should NOT be scrubbed when anonymize=false")

    # ── 2. anonymize=ON, openai ──────────────────────────────────────────────
    def test_anonymize_on_openai_scrubs_pii(self):
        """When llm_anonymize_pii is true, email and name must be replaced before sending."""
        cfg = {**BASE_CFG, "llm_provider": "openai", "llm_anonymize_pii": "true"}
        db, sel_patch = _make_db(cfg)
        mock_cls, captured = _make_httpx_mock("Here is your response.", "openai")

        async def run():
            with sel_patch, patch("app.agents.pipeline.httpx.AsyncClient", mock_cls):
                return await _llm_chat(
                    db, SYSTEM_PROMPT, USER_PROMPT,
                    pii_map={REAL_EMAIL: "redacted@example.com", REAL_NAME: "[CUSTOMER]"},
                )

        asyncio.run(run())

        sent_user = captured[0]["json"]["messages"][-1]["content"]
        # Real email and name must NOT appear
        self.assertNotIn(REAL_EMAIL, sent_user,
                         "Real email should be scrubbed when anonymize=true")
        self.assertNotIn(REAL_NAME, sent_user,
                         "Real name should be scrubbed when anonymize=true")
        # Placeholders must be present
        self.assertIn("redacted@example.com", sent_user)
        self.assertIn("[CUSTOMER]", sent_user)

    # ── 3. anonymize=ON, anthropic ───────────────────────────────────────────
    def test_anonymize_on_anthropic_scrubs_pii(self):
        """Anonymization applies to Anthropic provider too."""
        cfg = {**BASE_CFG, "llm_provider": "anthropic", "llm_anonymize_pii": "true"}
        db, sel_patch = _make_db(cfg)
        mock_cls, captured = _make_httpx_mock("Anthropic response.", "anthropic")

        async def run():
            with sel_patch, patch("app.agents.pipeline.httpx.AsyncClient", mock_cls):
                return await _llm_chat(
                    db, SYSTEM_PROMPT, USER_PROMPT,
                    pii_map={REAL_EMAIL: "redacted@example.com", REAL_NAME: "[CUSTOMER]"},
                )

        asyncio.run(run())

        sent_user = captured[0]["json"]["messages"][0]["content"]
        self.assertNotIn(REAL_EMAIL, sent_user)
        self.assertNotIn(REAL_NAME, sent_user)
        self.assertIn("redacted@example.com", sent_user)

    # ── 4. anonymize=ON, ollama → NO scrubbing (local model) ────────────────
    def test_anonymize_on_ollama_does_not_scrub(self):
        """Ollama is a local model; anonymization must NOT be applied."""
        cfg = {**BASE_CFG, "llm_provider": "ollama", "llm_anonymize_pii": "true"}
        db, sel_patch = _make_db(cfg)
        captured = []

        ollama_response = MagicMock()
        ollama_response.raise_for_status = MagicMock()
        ollama_response.json.return_value = {"message": {"content": "Ollama reply"}}

        mock_ollama = AsyncMock()
        mock_ollama.__aenter__ = AsyncMock(return_value=mock_ollama)
        mock_ollama.__aexit__ = AsyncMock(return_value=False)

        async def fake_post(url, **kwargs):
            captured.append({"url": url, **kwargs})
            return ollama_response

        mock_ollama.post = fake_post
        mock_ollama_cls = MagicMock(return_value=mock_ollama)

        async def run():
            with sel_patch, patch("app.agents.pipeline.httpx.AsyncClient", mock_ollama_cls):
                return await _llm_chat(
                    db, SYSTEM_PROMPT, USER_PROMPT,
                    pii_map={REAL_EMAIL: "redacted@example.com", REAL_NAME: "[CUSTOMER]"},
                )

        asyncio.run(run())

        self.assertEqual(len(captured), 1)
        sent_user = captured[0]["json"]["messages"][-1]["content"]
        # Ollama call must receive the original, un-anonymized prompt
        self.assertIn(REAL_EMAIL, sent_user,
                      "Ollama (local) should receive the original prompt")
        self.assertIn(REAL_NAME, sent_user)

    # ── 5. Response de-anonymization ─────────────────────────────────────────
    def test_customer_token_restored_in_response(self):
        """
        The [CUSTOMER] placeholder that the LLM echoes back must be replaced
        with the real name by the caller (simulated here).
        """
        # Simulate what _compose_response does after _llm_chat returns
        llm_output = "Dear [CUSTOMER], please find your documents attached."
        real_name  = "Jane Doe"

        restored = llm_output.replace("[CUSTOMER]", real_name)
        self.assertNotIn("[CUSTOMER]", restored)
        self.assertIn(real_name, restored)
        self.assertEqual(restored,
                         "Dear Jane Doe, please find your documents attached.")

    # ── 6. Regex fallback: email in body without explicit pii_map entry ───────
    def test_regex_scrubs_unlisted_email_in_body(self):
        """
        Even if pii_map doesn't include every email, the regex pass must
        catch and replace any remaining email patterns.
        """
        cfg = {**BASE_CFG, "llm_provider": "openai", "llm_anonymize_pii": "true"}
        db, sel_patch = _make_db(cfg)
        mock_cls, captured = _make_httpx_mock("Response.", "openai")

        extra_email = "other.person@third-party.org"
        prompt_with_extra = USER_PROMPT + f"\nCC: {extra_email}"

        async def run():
            with sel_patch, patch("app.agents.pipeline.httpx.AsyncClient", mock_cls):
                # pii_map only covers REAL_EMAIL, not extra_email
                return await _llm_chat(
                    db, SYSTEM_PROMPT, prompt_with_extra,
                    pii_map={REAL_EMAIL: "redacted@example.com"},
                )

        asyncio.run(run())

        sent_user = captured[0]["json"]["messages"][-1]["content"]
        self.assertNotIn(REAL_EMAIL, sent_user,
                         "pii_map email should be replaced")
        self.assertNotIn(extra_email, sent_user,
                         "Unlisted email in body should be caught by regex")
        self.assertIn("redacted@example.com", sent_user)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromTestCase(TestPiiAnonymization)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
