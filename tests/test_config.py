import pytest

from ccr.config import DEFAULTS, ConfigError, default_config, load_config


def write_cfg(root, text):
    p = root / ".ccr" / "config.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_defaults_when_missing(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.path is None and not cfg.errors and not cfg.warnings
    t = cfg.tiers
    assert [(t[k].model, t[k].effort, t[k].agent) for k in ("T0", "T1", "T2", "T3", "T4")] == [
        ("haiku", "low", "ccr-quick"), ("sonnet", "low", "ccr-small"),
        ("sonnet", "medium", "ccr-implement"), ("opus", "high", "ccr-architect"),
        ("opus", "xhigh", "ccr-critical")]
    assert "fable" not in str(cfg["tiers"])
    assert cfg.tier(4).name == "T4" and cfg.tier("T2").index == 2


def test_defaults_are_not_shared(tmp_path):
    a = default_config()
    a.data["tiers"]["T0"]["model"] = "opus"
    assert DEFAULTS["tiers"]["T0"]["model"] == "haiku"
    assert load_config(tmp_path).tier("T0").model == "haiku"


def test_partial_override_merges(tmp_path):
    write_cfg(tmp_path, "tiers:\n  T1: {effort: medium}\nbudget:\n  soft_limit_pct: 70\n"
                        "class_base:\n  feature: T1\n")
    cfg = load_config(tmp_path)
    assert not cfg.errors
    assert cfg.tier("T1").effort == "medium" and cfg.tier("T1").model == "sonnet"
    assert cfg.tier("T3").model == "opus"
    assert cfg["budget"]["soft_limit_pct"] == 70
    assert cfg["budget"]["state_max_age_hours"] == 24
    assert cfg["class_base"]["feature"] == "T1" and cfg["class_base"]["bugfix"] == "T1"


@pytest.mark.parametrize("yaml_text,needle", [
    ("tiers:\n  T4: {effort: max}\n", "effort must be one of low|medium|high|xhigh"),
    ("tiers:\n  T2: {model: gpt-4}\n", "unknown model"),
    ("tiers:\n  T2: {model: fable}\n", "fable is only allowed for T4"),
    ("tiers:\n  T5: {model: opus}\n", "unknown tier"),
    ("tiers:\n  T1: {agent: 'Bad Name'}\n", "agent must be"),
    ("class_base:\n  feature: T9\n", "expected T0..T4"),
    ("class_base:\n  chores: T1\n", "unknown class"),
    ("risk:\n  medium: 0.8\n  high: 0.5\n", "medium must be below high"),
    ("risk:\n  weights: {blast_radius: 2}\n", "risk.weights.blast_radius"),
    ("budget:\n  soft_limit_pct: 150\n", "budget.soft_limit_pct"),
    ("critical_paths:\n  auth: auth/**\n", "critical_paths.auth: expected a list"),
    ("llm:\n  effort: max\n", "llm.effort"),
    ("routing:\n  ceiling_without_critical: T7\n", "routing.ceiling_without_critical"),
    ("- just\n- a list\n", "top level must be a mapping"),
    ("tiers: [unclosed\n", "cannot parse"),
])
def test_validation_errors_keep_defaults(tmp_path, yaml_text, needle):
    write_cfg(tmp_path, yaml_text)
    cfg = load_config(tmp_path)
    assert any(needle in e for e in cfg.errors), cfg.errors
    d = default_config()
    assert cfg["tiers"] == d["tiers"]
    assert cfg["risk"]["medium"] == d["risk"]["medium"] and cfg["risk"]["high"] == d["risk"]["high"]
    with pytest.raises(ConfigError):
        load_config(tmp_path, strict=True)


def test_fable_allowed_for_t4_only_if_configured(tmp_path):
    write_cfg(tmp_path, "tiers:\n  T4: {model: fable, effort: xhigh}\n")
    cfg = load_config(tmp_path, strict=True)
    assert cfg.tier("T4").model == "fable" and not cfg.errors


def test_full_model_ids_accepted(tmp_path):
    write_cfg(tmp_path, "tiers:\n  T3: {model: claude-opus-5-5}\n  T2: {model: 'claude-sonnet-5[1m]'}\n")
    cfg = load_config(tmp_path, strict=True)
    assert cfg.tier("T3").model == "claude-opus-5-5"


def test_unknown_keys_warn(tmp_path):
    write_cfg(tmp_path, "version: 1\nfoo: 1\ntiers:\n  T0: {colour: red}\nrisk:\n  bar: 1\n")
    cfg = load_config(tmp_path, strict=True)
    assert not cfg.errors
    assert any("foo" in w for w in cfg.warnings)
    assert any("tiers.T0.colour" in w for w in cfg.warnings)
    assert any("risk.bar" in w for w in cfg.warnings)


def test_non_monotonic_tiers_warn(tmp_path):
    write_cfg(tmp_path, "tiers:\n  T2: {model: haiku, effort: low}\n")
    cfg = load_config(tmp_path)
    assert any("cheaper than the tier below" in w for w in cfg.warnings)


def test_critical_groups_replace_disable_and_add(tmp_path):
    write_cfg(tmp_path, "critical_paths:\n  auth: ['**/identity/**']\n  infra: []\n"
                        "  billing_v2: ['svc/billing2/**']\ncritical_terms:\n  auth: [Identity]\n")
    cfg = load_config(tmp_path, strict=True)
    assert cfg["critical_paths"]["auth"] == ["**/identity/**"]
    assert cfg["critical_paths"]["infra"] == []
    assert cfg["critical_paths"]["billing_v2"] == ["svc/billing2/**"]
    assert cfg["critical_paths"]["payments"] == DEFAULTS["critical_paths"]["payments"]
    assert cfg["critical_terms"]["auth"] == ["identity"]


def test_empty_file_is_defaults(tmp_path):
    write_cfg(tmp_path, "")
    cfg = load_config(tmp_path, strict=True)
    assert cfg["tiers"] == DEFAULTS["tiers"]


def test_config_drives_routing(tmp_path):
    from ccr.route import decide
    (tmp_path / "cli.py").write_text("def main():\n    pass\n")
    write_cfg(tmp_path, "class_base:\n  feature: T1\ntiers:\n  T1: {agent: my-small}\n")
    d = decide("Add a --verbose flag to cli.py", tmp_path, graph=None, memory=None,
               history_records=[])
    assert d.task_class.kind == "feature"
    assert d.tier.name == "T1" and d.tier.agent == "my-small"
    assert d.reasons[-1] == "final T1: sonnet/low via my-small"


def test_config_errors_surface_in_decision_notes(tmp_path):
    from ccr.route import decide
    write_cfg(tmp_path, "tiers:\n  T0: {effort: max}\n")
    d = decide("What is this?", tmp_path, graph=None, memory=None, history_records=[])
    assert d.tier.effort == "low"
    assert any(n.startswith("config: tiers.T0.effort") for n in d.notes)
