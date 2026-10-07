"""Tests for primary/fallback Gemini editing without network access."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from google.genai.errors import ClientError, ServerError

from postradar.services.ai_editor import AIEditor

PRIMARY_MODEL = "gemini-3.5-flash-lite"
FALLBACK_MODEL = "gemini-3.8-flash"


def fake_client(generate_content: AsyncMock) -> SimpleNamespace:
    return SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    )


def provider_error(error_type: type[Exception], status_code: int, message: str) -> Exception:
    return error_type(
        status_code,
        {
            "error": {
                "code": status_code,
                "message": message,
                "status": "UNAVAILABLE" if status_code >= 500 else "ERROR",
            }
        },
    )


class AIEditorTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_success_skips_fallback(self) -> None:
        generate = AsyncMock(return_value=SimpleNamespace(text="Edited Russian text"))
        editor = AIEditor(api_key="test-key", client=fake_client(generate))

        result = await editor.edit("Sanitized Russian text")

        self.assertEqual(result, "Edited Russian text")
        generate.assert_awaited_once()
        request = generate.await_args.kwargs
        self.assertEqual(request["model"], PRIMARY_MODEL)
        self.assertEqual(request["contents"], "Sanitized Russian text")
        self.assertIn("Preserve its original meaning", request["config"].system_instruction)

    async def test_primary_503_uses_fallback_and_redacts_secrets(self) -> None:
        api_key = "test-secret-api-key"
        error = provider_error(
            ServerError,
            503,
            "Temporary provider failure; Authorization: Bearer test-secret-api-key",
        )
        generate = AsyncMock(
            side_effect=[error, SimpleNamespace(text="Edited by fallback")]
        )
        editor = AIEditor(api_key=api_key, client=fake_client(generate))

        with self.assertLogs("postradar.services.ai_editor", level="WARNING") as captured:
            result = await editor.edit("Sanitized text")

        self.assertEqual(result, "Edited by fallback")
        self.assertEqual([call.kwargs["model"] for call in generate.await_args_list], [PRIMARY_MODEL, FALLBACK_MODEL])
        self.assertIn("trying fallback", captured.output[0])
        self.assertIn("status_code=503", captured.output[0])
        self.assertIn("Temporary provider failure", captured.output[0])
        self.assertNotIn(api_key, captured.output[0])
        self.assertIn("Authorization: Bearer [REDACTED]", captured.output[0])

    async def test_primary_429_uses_fallback(self) -> None:
        generate = AsyncMock(
            side_effect=[
                provider_error(ClientError, 429, "Rate limit reached"),
                SimpleNamespace(text="Fallback edit"),
            ]
        )
        editor = AIEditor(api_key="test-key", client=fake_client(generate))
        self.assertEqual(await editor.edit("Sanitized text"), "Fallback edit")
        self.assertEqual([call.kwargs["model"] for call in generate.await_args_list], [PRIMARY_MODEL, FALLBACK_MODEL])

    async def test_primary_500_uses_fallback(self) -> None:
        generate = AsyncMock(
            side_effect=[
                provider_error(ServerError, 500, "Internal server error"),
                SimpleNamespace(text="Fallback edit"),
            ]
        )
        editor = AIEditor(api_key="test-key", client=fake_client(generate))
        self.assertEqual(await editor.edit("Sanitized text"), "Fallback edit")
        self.assertEqual([call.kwargs["model"] for call in generate.await_args_list], [PRIMARY_MODEL, FALLBACK_MODEL])

    async def test_both_models_fail_falls_back_to_sanitized_text(self) -> None:
        generate = AsyncMock(
            side_effect=[
                provider_error(ServerError, 503, "Primary unavailable"),
                provider_error(ServerError, 502, "Fallback unavailable"),
            ]
        )
        editor = AIEditor(api_key="test-key", client=fake_client(generate))

        with self.assertLogs("postradar.services.ai_editor", level="WARNING") as captured:
            result = await editor.edit("Sanitized text")

        self.assertEqual(result, "Sanitized text")
        self.assertEqual(generate.await_count, 2)
        self.assertIn("AI fallback model failed; using sanitized text", captured.output[-1])

    async def test_permanent_auth_error_does_not_call_fallback(self) -> None:
        generate = AsyncMock(
            side_effect=provider_error(ClientError, 401, "Invalid API key")
        )
        editor = AIEditor(api_key="test-key", client=fake_client(generate))

        with self.assertLogs("postradar.services.ai_editor", level="WARNING") as captured:
            result = await editor.edit("Sanitized text")

        self.assertEqual(result, "Sanitized text")
        generate.assert_awaited_once()
        self.assertEqual(generate.await_args.kwargs["model"], PRIMARY_MODEL)
        self.assertIn("AI primary model failed; using sanitized text", captured.output[0])

    async def test_empty_primary_response_falls_back_safely_without_second_request(self) -> None:
        generate = AsyncMock(return_value=SimpleNamespace(text=" \n"))
        editor = AIEditor(api_key="test-key", client=fake_client(generate))

        with self.assertLogs("postradar.services.ai_editor", level="WARNING"):
            result = await editor.edit("Sanitized text")

        self.assertEqual(result, "Sanitized text")
        generate.assert_awaited_once()

    async def test_disabled_editor_returns_input_without_api_call(self) -> None:
        generate = AsyncMock()
        editor = AIEditor(api_key="", enabled=False, client=fake_client(generate))
        self.assertEqual(await editor.edit("Sanitized text"), "Sanitized text")
        generate.assert_not_awaited()

    async def test_missing_key_disables_editor_and_returns_input(self) -> None:
        generate = AsyncMock()
        with self.assertLogs("postradar.services.ai_editor", level="WARNING"):
            editor = AIEditor(api_key="", enabled=True, client=fake_client(generate))
        self.assertEqual(await editor.edit("Sanitized text"), "Sanitized text")
        generate.assert_not_awaited()

    async def test_empty_input_does_not_call_api(self) -> None:
        generate = AsyncMock()
        editor = AIEditor(api_key="test-key", client=fake_client(generate))
        self.assertEqual(await editor.edit("  "), "  ")
        generate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
