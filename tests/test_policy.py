import pytest

from conductor.models import ClassificationConfig, WorkflowSpec
from conductor.policy import classify_workflow


def wf(*tasks):
    return WorkflowSpec.model_validate({"name": "t", "tasks": list(tasks)})


def test_path_rule_raises_level():
    cfg = ClassificationConfig.model_validate({"paths": [{"glob": "payment/signing/**", "level": "L3"}]})
    spec = wf({"id": "a", "title": "x", "paths": ["payment/signing/hmac.py"]})
    c = classify_workflow(spec.tasks, cfg)["a"]
    assert c.level == "L3"
    assert any("payment/signing/**" in r for r in c.reasons)


@pytest.mark.parametrize(
    "text, level",
    [
        ("use api_key = sk-abcdef123456", "L3"),
        ("-----BEGIN RSA PRIVATE KEY-----", "L3"),
        ("customer phone 13812345678", "L2"),
        ("id 11010519491231002X", "L2"),
        ("refactor the button colors", "L0"),
    ],
)
def test_builtin_content_detectors(text, level):
    spec = wf({"id": "a", "title": "t", "description": text})
    assert classify_workflow(spec.tasks, ClassificationConfig())["a"].level == level


def test_classification_never_lowers_declared_level():
    spec = wf({"id": "a", "title": "harmless", "level": "L3"})
    assert classify_workflow(spec.tasks, ClassificationConfig())["a"].level == "L3"


def test_level_propagates_along_inputs_but_not_ordering_only_deps():
    spec = wf(
        {"id": "secret", "title": "s", "level": "L3"},
        {"id": "reads", "title": "r", "depends_on": ["secret"]},
        {"id": "after", "title": "o", "depends_on": ["secret"], "inputs": []},
        {"id": "transitive", "title": "t", "depends_on": ["reads"]},
    )
    levels = {k: v.level for k, v in classify_workflow(spec.tasks, ClassificationConfig()).items()}
    assert levels == {"secret": "L3", "reads": "L3", "after": "L0", "transitive": "L3"}


def test_workflow_rejects_cycles_and_unknown_inputs():
    with pytest.raises(ValueError, match="cycle"):
        wf({"id": "a", "title": "a", "depends_on": ["b"]}, {"id": "b", "title": "b", "depends_on": ["a"]})
    with pytest.raises(ValueError, match="must also be in depends_on"):
        wf({"id": "a", "title": "a"}, {"id": "b", "title": "b", "inputs": ["a"]})
