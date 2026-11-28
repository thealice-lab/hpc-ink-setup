from __future__ import annotations

from dataclasses import replace

import pytest

from inkly.plugins.manager import PluginManager
from inkly.retrieval.embedding import TfidfEmbedder
from inkly.retrieval.retriever import PluginRetriever


@pytest.fixture(scope="module")
def plugins():
    return PluginManager().discover()


@pytest.fixture
def select(plugins, tmp_path):
    retriever = PluginRetriever(
        index_path=tmp_path / "retrieval.json", top_k=3, min_score=0.01
    )

    def names(query):
        return [plugin.name for plugin in retriever.select_plugins(query, plugins)]

    return names


@pytest.mark.parametrize(
    ("query", "plugin"),
    [
        ("What are my jobs doing right now?", "my_jobs"),
        ("is my job running", "my_jobs"),
        ("why is my job pending?", "my_jobs"),
        ("when will my job start", "my_jobs"),
        ("how much time is left on my job", "my_jobs"),
        ("why is job 123456 still waiting", "my_jobs"),
        ("what jobs do I have in the queue", "my_jobs"),
        ("how did my recent jobs go", "my_recent_jobs"),
        ("did my last job finish", "my_recent_jobs"),
        ("why did my job fail", "my_recent_jobs"),
        ("how efficient were my jobs", "my_recent_jobs"),
        ("how much memory did my last job use", "my_recent_jobs"),
        ("am I asking for too many CPUs", "my_recent_jobs"),
        ("How busy is the cluster right now?", "queue_status"),
        ("how many idle nodes are there", "queue_status"),
        ("how many CPUs are free", "queue_status"),
        ("is the gpu partition busy", "queue_status"),
        ("what partitions are available", "node_info"),
        ("what is the max walltime", "node_info"),
        ("which nodes have GPUs", "node_info"),
    ],
)
def test_slurm_questions_reach_the_plugin_that_answers_them(select, query, plugin):
    assert plugin in select(query)


@pytest.mark.parametrize("query", ["hello", "what is 2+2", "thanks!"])
def test_unrelated_questions_run_no_cluster_commands(select, query):
    assert select(query) == []


def test_gaussian_questions_still_reach_the_documentation(select):
    selected = select("How do I run Gaussian on this cluster?")

    assert "docs_gaussian" in selected
    assert not {"my_jobs", "my_recent_jobs"} & set(selected)


def test_common_words_do_not_count_but_personal_ones_do():
    tokens = TfidfEmbedder.tokenize("What is the state of my job and why do I wait?")

    assert tokens == ["state", "my", "job", "why", "i", "wait"]


def test_index_is_rebuilt_when_plugin_text_changes(plugins, tmp_path):
    index = tmp_path / "retrieval.json"
    PluginRetriever(index_path=index).select_plugins("is my job running", plugins)

    edited = dict(plugins)
    edited["node_info"] = replace(
        plugins["node_info"],
        example_queries=[*plugins["node_info"].example_queries, "zebrafish nodes"],
    )
    selected = PluginRetriever(index_path=index).select_plugins("zebrafish", edited)

    assert [plugin.name for plugin in selected] == ["node_info"]
