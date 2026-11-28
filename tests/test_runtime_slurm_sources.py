from __future__ import annotations

from types import SimpleNamespace

from inkly import ink_core
from inkly.core.runtime import InklyRuntime
from tests.test_runtime import (
    FakeBackend,
    FakeConversationManager,
    FakePlugin,
    FakePluginManager,
    make_config,
)

SQUEUE = "squeue --user $USER --format '%i|%T'"
SINFO = "sinfo --format '%P|%a|%F|%C'"


def runtime_with(
    monkeypatch, outputs, *, response="Two jobs are running.", config=None
):
    config = config or make_config()
    conversation = FakeConversationManager(config)
    plugins = FakePluginManager(
        {name: FakePlugin(output) for name, output in outputs.items()}
    )
    backend = FakeBackend(response=response)
    monkeypatch.setattr(
        "inkly.core.runtime.ConversationManager", lambda cfg: conversation
    )
    monkeypatch.setattr("inkly.core.runtime.PluginManager", lambda: plugins)
    monkeypatch.setattr("inkly.core.runtime.LLMBackend", lambda cfg: backend)
    return InklyRuntime(config), conversation, backend


def test_slurm_commands_are_listed_after_the_answer_not_shown_to_the_model(
    monkeypatch,
):
    runtime, conversation, backend = runtime_with(
        monkeypatch,
        {
            "my_jobs": f"Your Slurm Jobs (live)\n- 1 running\nSlurm command: {SQUEUE}",
            "queue_status": (
                f"Queue Status\nRunning jobs: 3\nSlurm command: {SINFO}\n"
                f"Slurm command: {SQUEUE}"
            ),
        },
    )

    response = runtime.handle_query("user1", "what are my jobs doing?")

    prompt = backend.prompts[0]
    assert "Slurm command:" not in prompt
    assert "--format" not in prompt
    assert "- 1 running" in prompt
    assert response == (
        "Two jobs are running.\n\n"
        "From these read-only Slurm commands:\n"
        f"  {SQUEUE}\n"
        f"  {SINFO}"
    )
    assert conversation.appended[-1] == ("user1", "assistant", "Two jobs are running.")


def test_answers_without_live_slurm_data_are_unchanged(monkeypatch):
    runtime, _, _ = runtime_with(monkeypatch, {"docs_gaussian": "Gaussian notes"})

    assert runtime.handle_query("user1", "gaussian?") == "Two jobs are running."


def test_source_note_splits_back_out():
    response = InklyRuntime._with_source_note("Answer.\n", [SQUEUE])

    answer, note = InklyRuntime.split_source_note(response)

    assert answer == "Answer."
    assert note == f"From these read-only Slurm commands:\n  {SQUEUE}"
    assert InklyRuntime.split_source_note("Plain answer") == ("Plain answer", "")


def test_live_plugin_context_survives_a_long_history(monkeypatch):
    runtime, _, _ = runtime_with(monkeypatch, {}, config=make_config(4000))
    history = [f"user: old question {n} " + "x" * 80 for n in range(60)]
    live = "Your Slurm Jobs (live)\n" + "\n".join(f"- job {n}" for n in range(40))

    prompt = runtime.assemble_prompt(
        query="why is my job pending?",
        history_lines=history,
        plugin_outputs={"my_jobs": live},
    )

    assert len(prompt) <= 4000
    assert live in prompt
    assert history[-1] in prompt
    assert history[0] not in prompt
    assert prompt.index("=== CONVERSATION HISTORY ===") < prompt.index(
        "=== PLUGIN CONTEXT ==="
    )
    assert prompt.rstrip().endswith("why is my job pending?")


def test_terminal_prints_the_source_note_after_a_streamed_answer(monkeypatch, capsys):
    response = InklyRuntime._with_source_note("Streamed answer", [SQUEUE])

    class StreamingRuntime:
        CLUSTER_SCOPE_WITHHELD_RESPONSE = "withheld"
        split_source_note = staticmethod(InklyRuntime.split_source_note)

        def __init__(self, cfg):
            pass

        def handle_query(self, user_id, query):
            print("Streamed answer", end="")
            return response

    monkeypatch.setattr(ink_core, "_load_config", lambda: SimpleNamespace())
    monkeypatch.setattr(ink_core, "InklyRuntime", StreamingRuntime)
    monkeypatch.setattr(ink_core.sys, "argv", ["ink", "my", "jobs"])
    monkeypatch.setattr(ink_core.sys.stdout, "isatty", lambda: True)

    assert ink_core.main() == 0

    assert capsys.readouterr().out == (
        f"Streamed answer\n\nFrom these read-only Slurm commands:\n  {SQUEUE}\n"
    )
