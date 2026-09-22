from tusker_gateway.question_adapters import (
    adapter_for_name,
    detect_adapter,
    render_question_arguments,
    supported_names,
)


def test_top_harness_profiles_are_registered():
    assert set(supported_names()) >= {
        "omp", "opencode", "cline", "roo", "continue",
        "cursor", "claude_code", "codex_cli", "gemini_cli", "aider",
    }


def test_explicit_harness_header_wins_over_user_agent():
    adapter = detect_adapter(
        {"X-Tusker-Harness": "opencode", "User-Agent": "Cline/3"},
        {},
    )
    assert adapter.key == "opencode"
    assert adapter.tool_name == "question"


def test_user_agent_detection_is_conservative():
    assert detect_adapter({"User-Agent": "continue-cli/1.0"}, {}).key == "continue"
    assert detect_adapter({"User-Agent": "unknown-client/1.0"}, {}).key == "omp"


def test_cline_shape_keeps_question_id_and_followup_options():
    adapter = adapter_for_name("cline")
    result = render_question_arguments(
        adapter,
        question_id="approval-1",
        prompt="Approve this?",
        header="Approval",
        options=[{"label": "Allow once", "description": "Run once."}],
    )
    assert result["question_id"] == "approval-1"
    assert result["question"] == "Approve this?"
    assert result["follow_up"][0]["text"] == "Allow once"


def test_continue_shape_uses_options_envelope():
    adapter = adapter_for_name("continue")
    result = render_question_arguments(
        adapter,
        question_id="approval-2",
        prompt="Approve this?",
        header="Approval",
        options=[{"label": "Allow once", "description": "Run once."}],
    )
    assert result == {
        "id": "approval-2",
        "question": "Approve this?",
        "header": "Approval",
        "options": [{"label": "Allow once", "description": "Run once."}],
    }


def test_opencode_uses_question_tool_and_omp_questions_shape():
    adapter = adapter_for_name("opencode")
    result = render_question_arguments(
        adapter,
        question_id="approval-3",
        prompt="Approve this?",
        header="Approval",
        options=[],
    )
    assert adapter.tool_name == "question"
    assert result["questions"][0]["id"] == "approval-3"
