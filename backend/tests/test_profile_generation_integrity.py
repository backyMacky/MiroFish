"""Provider failure and concurrency boundaries, without a semantic graph substitute."""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from app.services import oasis_profile_generator as module
from app.services import simulation_config_generator as config_module
from app.services.zep_entity_reader import EntityNode


@pytest.fixture
def generator(monkeypatch):
    monkeypatch.setattr(module.Config, "GRAPH_BACKEND", "zep_cloud")
    monkeypatch.setattr(module.Config, "ZEP_API_KEY", None)
    monkeypatch.setenv("LLM_MAX_CONCURRENT_REQUESTS", "1")
    monkeypatch.setenv("PROFILE_PERSONA_CHARACTERS", "600")
    monkeypatch.setenv("PROFILE_MAX_TOKENS", "2048")
    return module.OasisProfileGenerator(
        api_key="synthetic",
        base_url="http://127.0.0.1:9/v1",
        source_context="Avery Chen is the procurement director at Harbor Labs.",
    )


def entity(name):
    return EntityNode(
        uuid=name,
        name=name,
        labels=["Entity", "Person"],
        summary="A synthetic participant",
        attributes={},
    )


@pytest.mark.parametrize(
    "entity_type, individual",
    [
        ("ProcurementDirector", True),
        ("SupplierAccountManager", True),
        ("ChiefFinancialOfficer", True),
        ("GraduateStudent", True),
        ("CommunityRepresentative", True),
        ("TaxLawyer", True),
        ("ResearchScientist", True),
        ("HumanResourcesCoordinator", True),
        ("Person", True),
        ("ProcurementDepartment", False),
        ("BoardOfDirectors", False),
        ("CommunityOrganization", False),
        ("Directorate", False),
        ("ResearchUniversity", False),
        ("GovernmentAgency", False),
        ("PersonalityResearchGroup", False),
    ],
)
def test_custom_occupational_types_use_personas_instead_of_corporate_accounts(
    generator, entity_type, individual
):
    assert generator._is_individual_entity(entity_type) is individual


def completion(*, truncated=False):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="length" if truncated else "stop",
                message=SimpleNamespace(
                    content=json.dumps(
                        {"bio": "Synthetic", "persona": "A complete simulated persona."}
                    )
                ),
            )
        ]
    )


def test_truncation_is_not_repaired_into_a_completed_persona(generator, monkeypatch):
    monkeypatch.setattr(
        module, "create_chat_completion", lambda *_args, **_kwargs: completion(truncated=True)
    )
    with pytest.raises(RuntimeError, match="persona generation failed"):
        generator.generate_profile_from_entity(entity("Avery"), 0, use_llm=True)


def test_provider_failure_cannot_silently_substitute_rule_based_personas(generator, monkeypatch):
    calls = []

    def fail(*_args, **_kwargs):
        calls.append(True)
        raise RuntimeError("PRIVATE PROVIDER RESPONSE")

    monkeypatch.setattr(module, "create_chat_completion", fail)
    with pytest.raises(RuntimeError, match="no rule-based personas") as raised:
        generator.generate_profiles_from_entities(
            [entity("Avery"), entity("Morgan")], use_llm=True, parallel_count=5
        )
    assert len(calls) == 1
    assert "PRIVATE" not in str(raised.value)


def test_configured_concurrency_and_source_grounding_are_preserved(generator, monkeypatch):
    lock = threading.Lock()
    active = 0
    maximum = 0
    requests = []

    def succeed(*_args, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            requests.append(kwargs)
        time.sleep(0.02)
        with lock:
            active -= 1
        return completion()

    monkeypatch.setattr(module, "create_chat_completion", succeed)
    profiles = generator.generate_profiles_from_entities(
        [entity("Avery"), entity("Morgan")], use_llm=True, parallel_count=5
    )
    assert maximum == 1 and len(profiles) == 2
    assert all(request["max_tokens"] == 2048 for request in requests)
    assert all("procurement director" in request["messages"][1]["content"] for request in requests)


def test_explicit_rule_based_mode_remains_available(generator, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Rule-based mode must not call the model")

    monkeypatch.setattr(module, "create_chat_completion", forbidden)
    profiles = generator.generate_profiles_from_entities([entity("Avery")], use_llm=False)
    assert len(profiles) == 1 and profiles[0].name == "Avery"


@pytest.mark.parametrize("stage", ["time", "event", "agents"])
def test_configuration_provider_failure_cannot_become_default_success(generator, monkeypatch, stage):
    config = config_module.SimulationConfigGenerator(
        api_key="synthetic", base_url="http://127.0.0.1:9/v1",
    )
    calls = []
    def fail(*_args, **_kwargs):
        calls.append(True)
        raise RuntimeError("PRIVATE PROVIDER RESPONSE")
    monkeypatch.setattr(config_module, "create_chat_completion", fail)
    with pytest.raises(RuntimeError, match="AI simulation configuration failed") as raised:
        if stage == "time":
            config._generate_time_config("context", 1)
        elif stage == "event":
            config._generate_event_config("context", "requirement", [entity("Avery")])
        else:
            config._generate_agent_configs_batch("context", [entity("Avery")], 0, "requirement")
    assert len(calls) == 1 and "PRIVATE" not in str(raised.value)


def test_configuration_truncation_and_missing_agent_are_not_success(generator, monkeypatch):
    config = config_module.SimulationConfigGenerator(
        api_key="synthetic", base_url="http://127.0.0.1:9/v1",
    )
    monkeypatch.setattr(config_module, "create_chat_completion", lambda *_a, **_k: completion(truncated=True))
    with pytest.raises(RuntimeError, match="AI simulation configuration failed"):
        config._generate_time_config("context", 1)
    monkeypatch.setattr(config, "_call_llm_with_retry", lambda *_a: {"agent_configs": []})
    with pytest.raises(RuntimeError, match="omitted or duplicated"):
        config._generate_agent_configs_batch("context", [entity("Avery")], 0, "requirement")
