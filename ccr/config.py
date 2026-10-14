"""`.ccr/config.yaml` loader: built-in defaults, deep merge, validation.

Invalid values never crash a hook: each bad field keeps its default and an error message is
recorded on `Config.errors` (or `ConfigError` is raised with `strict=True`). PyYAML is only
imported when a config file exists.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path

TIERS = ("T0", "T1", "T2", "T3", "T4")
# The `effortLevel` setting accepts only these (subagent frontmatter also allows `max`).
EFFORTS = ("low", "medium", "high", "xhigh")
MODEL_ALIASES = ("haiku", "sonnet", "opus", "fable")
CLASSES = ("question", "trivial", "tests", "bugfix", "feature", "refactor", "perf", "migration",
           "security", "debugging")
_MODEL_ID = re.compile(r"^claude-[a-z0-9][a-z0-9.\-]*(\[1m\])?$")
_AGENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

DEFAULTS: dict = {
    "tiers": {
        "T0": {"model": "haiku", "effort": "low", "agent": "ccr-quick"},
        "T1": {"model": "sonnet", "effort": "low", "agent": "ccr-small"},
        "T2": {"model": "sonnet", "effort": "medium", "agent": "ccr-implement"},
        "T3": {"model": "opus", "effort": "high", "agent": "ccr-architect"},
        "T4": {"model": "opus", "effort": "xhigh", "agent": "ccr-critical"},
    },
    # Starting tier per task class (before scope / risk / history / budget adjustments).
    "class_base": {
        "question": "T0", "trivial": "T0", "tests": "T1", "bugfix": "T1", "feature": "T2",
        "refactor": "T2", "perf": "T2", "migration": "T3", "security": "T3", "debugging": "T3",
    },
    # Critical-path globs per group. `**` spans directories, `*` does not; a pattern without
    # `/` matches the basename. Matching is case-insensitive. Set a group to [] to disable it.
    "critical_paths": {
        "auth": ["**/auth/**", "**/auth.*", "**/auth_*", "**/*_auth.*", "**/*auth.*",
                 "**/authn*", "**/authz*", "**/authenticat*", "**/authoriz*", "**/login*",
                 "**/session*", "**/oauth*", "**/jwt*", "**/permission*", "**/rbac*",
                 "**/acl.*", "**/password*", "**/credential*"],
        "payments": ["**/payment*/**", "**/payment*", "**/billing/**", "**/billing*",
                     "**/checkout*", "**/invoice*", "**/stripe*", "**/ledger*", "**/refund*",
                     "**/subscription*"],
        "migrations": ["**/migrations/**", "**/migrate/**", "**/alembic/**", "**/*.sql",
                       "**/schema.prisma", "**/db/schema*"],
        "infra": ["**/terraform/**", "**/*.tf", "**/*.tfvars", "**/k8s/**", "**/kubernetes/**",
                  "**/helm/**", "**/Dockerfile*", "**/docker-compose*", "**/.github/workflows/**",
                  "**/deploy/**", "**/infra/**", "**/ansible/**", "**/.gitlab-ci*"],
        "secrets": ["**/secret*", "**/crypto*", "**/*.pem", "**/*.key", "**/.env*"],
    },
    # Prompt words/phrases that put a task on a critical path even before files are located.
    # Matched as whole words (simple plurals included), case-insensitive.
    "critical_terms": {
        # bare "session" is ordinary wording ("in this session"); only security senses count
        "auth": ["auth", "authentication", "authorization", "login", "logout",
                 "session token", "session cookie", "session cookies", "session id", "session key",
                 "session secret", "session fixation", "session hijack",
                 "session management", "session expiry", "session expiration",
                 "session invalidation", "oauth", "jwt", "sso", "saml", "permission", "rbac",
                 "password", "credential"],
        "payments": ["payment", "billing", "invoice", "checkout", "refund", "stripe", "ledger",
                     "charge", "subscription", "payout"],
        "migrations": ["migration", "alembic", "schema change", "schema migration",
                       "database schema", "db schema", "alter table", "drop column",
                       "drop table", "backfill"],
        "infra": ["terraform", "kubernetes", "k8s", "helm", "dockerfile", "deployment",
                  "infrastructure", "infra", "ci pipeline", "github actions", "s3 bucket",
                  "iam", "vpc"],
        "secrets": ["secret", "encryption", "crypto", "certificate", "tls", "api key",
                    "private key", "signing key"],
    },
    # Project words that make a bug/perf task a hard problem (like "intermittent"): added to
    # the built-in list in ccr/evaluate/classify.py. Matched as whole words, case-insensitive.
    "hard_terms": [],
    # Blended USD per million tokens by model family, only used for the savings estimate in
    # `ccr stats` (interactive sessions report tokens, not dollars). Set your own rates.
    "pricing": {"haiku": 2.0, "sonnet": 6.0, "opus": 30.0},
    "risk": {
        # Max contribution of each component to the (noisy-or) risk score.
        "weights": {"blast_radius": 0.6, "complexity": 0.5, "test_proximity": 0.3,
                    "churn": 0.3, "critical_path": 0.7, "memory": 0.7},
        "medium": 0.3,           # score >= medium -> "medium"
        "high": 0.55,            # score >= high -> "high" (+1 tier for change tasks)
        "t4_code_risk": 0.65,    # code-only risk (no critical/memory) for the T4 rule
        "blast_full": 32,        # dependents count that saturates blast radius
        "churn_full_commits": 20,
        "complexity_full_branches": 15,
    },
    "budget": {
        "soft_limit_pct": 80,        # weekly usage % at which pressure starts
        "state_max_age_hours": 24,   # ignore an older .ccr/local/state.json
        "weekly_token_limit": None,  # enables the token fallback when no statusline data
    },
    "history": {
        "min_samples": 3,
        "fail_rate_up": 0.5,         # >= this failure rate at the chosen tier -> +1
        "success_rate_down": 0.8,    # >= this success rate one tier lower -> -1
        "window_days": 90,
        "explore_down": True,        # chosen tier keeps succeeding on low-risk simple
                                     # tasks -> try one tier lower (ccr/history/learn.py)
    },
    "llm": {
        "threshold": 0.5,            # rule confidence below this may ask the LLM (if allowed)
        "model": "haiku",
        "effort": "low",
        "timeout_s": 45,
    },
    "routing": {
        "max_escalation": 2,         # tiers above the class base without a T4 rule (2nd tier
                                     # only when 3+ independent signals agree)
        "ceiling_without_critical": "T3",
    },
    # `ccr run` (headless `claude -p`).
    "run": {
        "test_command": None,        # shell command that must pass for success, e.g. "pytest -q"
        "test_timeout_s": 900,
        "max_escalations": 1,        # tiers to climb after a failed attempt (0 = never retry)
        "max_turns": 40,             # per attempt (--max-turns)
        "max_budget_usd": None,      # total for the run across attempts (--max-budget-usd)
        "timeout_s": 3600,           # wall clock per attempt
        "permission_mode": None,     # passed as --permission-mode (e.g. acceptEdits)
    },
    # Claude Code hooks installed by `ccr init`. All hooks fail open (silent, exit 0).
    "hooks": {
        "enabled": True,             # master switch for every ccr hook
        "route_prompts": True,       # UserPromptSubmit: inject the routing line
        "show_in_ui": True,          # also show a short routing line to the USER (systemMessage)
        "model_advice": True,        # suggest `/model <x>` for iterative work above the main model
        "background_graph": True,    # SessionStart: refresh a stale graph in the background
        "watchdog_ms": 1500,         # hard internal deadline per hook run
    },
}


class ConfigError(ValueError):
    pass


@dataclass
class Tier:
    name: str
    model: str
    effort: str
    agent: str

    @property
    def index(self) -> int:
        return TIERS.index(self.name)


@dataclass
class Config:
    data: dict
    path: Path | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def tiers(self) -> dict[str, Tier]:
        return {k: Tier(k, v["model"], v["effort"], v["agent"])
                for k, v in self.data["tiers"].items()}

    def tier(self, name_or_index: str | int) -> Tier:
        name = TIERS[name_or_index] if isinstance(name_or_index, int) else name_or_index
        v = self.data["tiers"][name]
        return Tier(name, v["model"], v["effort"], v["agent"])

    def __getitem__(self, key: str):
        return self.data[key]


def config_path(root: str | Path) -> Path:
    return Path(root) / ".ccr" / "config.yaml"


def default_config() -> Config:
    return Config(copy.deepcopy(DEFAULTS))


def load_config(root: str | Path | None = None, *, path: str | Path | None = None,
                strict: bool = False) -> Config:
    """Defaults merged with `<root>/.ccr/config.yaml` (or `path`), validated."""
    p = Path(path) if path is not None else (config_path(root) if root is not None else None)
    cfg = default_config()
    if p is None or not p.exists():
        return cfg
    cfg.path = p
    try:
        import yaml
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception as e:  # yaml.YAMLError, OSError, UnicodeDecodeError
        cfg.errors.append(f"{p}: cannot parse: {e}".replace("\n", " "))
        raw = None
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        cfg.errors.append(f"{p}: top level must be a mapping")
        raw = {}
    _merge_validated(cfg, raw)
    if strict and cfg.errors:
        raise ConfigError("; ".join(cfg.errors))
    return cfg


# --------------------------------------------------------------------------- validation
def _valid_model(m: object) -> bool:
    return isinstance(m, str) and (m in MODEL_ALIASES or bool(_MODEL_ID.match(m)))


def _num(cfg: Config, where: str, v: object, lo: float, hi: float) -> bool:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        cfg.errors.append(f"{where}: expected a number in [{lo}, {hi}], got {v!r}")
        return False
    return True


def _str_list(cfg: Config, where: str, v: object) -> bool:
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        cfg.errors.append(f"{where}: expected a list of non-empty strings")
        return False
    return True


def _merge_validated(cfg: Config, raw: dict) -> None:
    d = cfg.data
    for key in raw:
        if key not in DEFAULTS and key != "version":
            cfg.warnings.append(f"unknown config key {key!r} ignored")

    tiers = raw.get("tiers")
    if tiers is not None:
        if not isinstance(tiers, dict):
            cfg.errors.append("tiers: expected a mapping of T0..T4")
        else:
            for name, spec in tiers.items():
                if name not in TIERS:
                    cfg.errors.append(f"tiers.{name}: unknown tier (expected T0..T4)")
                    continue
                if not isinstance(spec, dict):
                    cfg.errors.append(f"tiers.{name}: expected a mapping")
                    continue
                for k, v in spec.items():
                    where = f"tiers.{name}.{k}"
                    if k == "model":
                        if not _valid_model(v):
                            cfg.errors.append(f"{where}: unknown model {v!r} (use "
                                              f"{'/'.join(MODEL_ALIASES)} or a claude-* id)")
                        elif "fable" in str(v) and name != "T4":
                            cfg.errors.append(f"{where}: fable is only allowed for T4")
                        else:
                            d["tiers"][name]["model"] = v
                    elif k == "effort":
                        if v not in EFFORTS:
                            cfg.errors.append(f"{where}: effort must be one of "
                                              f"{'|'.join(EFFORTS)} (effortLevel), got {v!r}")
                        else:
                            d["tiers"][name]["effort"] = v
                    elif k == "agent":
                        if not isinstance(v, str) or not _AGENT.match(v):
                            cfg.errors.append(f"{where}: agent must be lowercase letters, "
                                              f"digits and dashes, got {v!r}")
                        else:
                            d["tiers"][name]["agent"] = v
                    else:
                        cfg.warnings.append(f"{where}: unknown field ignored")
            _check_monotonic(cfg)

    cb = raw.get("class_base")
    if cb is not None:
        if not isinstance(cb, dict):
            cfg.errors.append("class_base: expected a mapping")
        else:
            for k, v in cb.items():
                if k not in CLASSES:
                    cfg.errors.append(f"class_base.{k}: unknown class "
                                      f"(expected one of {', '.join(CLASSES)})")
                elif v not in TIERS:
                    cfg.errors.append(f"class_base.{k}: expected T0..T4, got {v!r}")
                else:
                    d["class_base"][k] = v

    for key in ("critical_paths", "critical_terms"):
        groups = raw.get(key)
        if groups is None:
            continue
        if not isinstance(groups, dict):
            cfg.errors.append(f"{key}: expected a mapping of group -> list")
            continue
        for g, items in groups.items():
            if items is None:
                items = []
            if _str_list(cfg, f"{key}.{g}", items):
                d[key][str(g)] = [x.strip() if key == "critical_paths" else x.strip().lower()
                                  for x in items]

    ht = raw.get("hard_terms")
    if ht is not None:
        if ht is None or _str_list(cfg, "hard_terms", ht):
            d["hard_terms"] = [x.strip().lower() for x in ht if x.strip()]
    pricing = raw.get("pricing")
    if pricing is not None:
        if not isinstance(pricing, dict):
            cfg.errors.append("pricing: expected a mapping of model -> USD per Mtok")
        else:
            for k, v in pricing.items():
                if k not in DEFAULTS["pricing"]:
                    cfg.warnings.append(f"pricing.{k}: unknown model ignored")
                elif _num(cfg, f"pricing.{k}", v, 0, 100_000):
                    d["pricing"][k] = float(v)

    risk = raw.get("risk")
    if risk is not None:
        if not isinstance(risk, dict):
            cfg.errors.append("risk: expected a mapping")
        else:
            for k, v in risk.items():
                if k == "weights":
                    if not isinstance(v, dict):
                        cfg.errors.append("risk.weights: expected a mapping")
                        continue
                    for wk, wv in v.items():
                        if wk not in DEFAULTS["risk"]["weights"]:
                            cfg.errors.append(f"risk.weights.{wk}: unknown component")
                        elif _num(cfg, f"risk.weights.{wk}", wv, 0, 1):
                            d["risk"]["weights"][wk] = float(wv)
                elif k in ("medium", "high", "t4_code_risk"):
                    if _num(cfg, f"risk.{k}", v, 0, 1):
                        d["risk"][k] = float(v)
                elif k in ("blast_full", "churn_full_commits", "complexity_full_branches"):
                    if _num(cfg, f"risk.{k}", v, 1, 1_000_000):
                        d["risk"][k] = v
                else:
                    cfg.warnings.append(f"risk.{k}: unknown field ignored")
            if d["risk"]["medium"] >= d["risk"]["high"]:
                cfg.errors.append("risk: medium must be below high; using defaults")
                d["risk"]["medium"], d["risk"]["high"] = (DEFAULTS["risk"]["medium"],
                                                          DEFAULTS["risk"]["high"])

    _section(cfg, raw, "budget", {
        "soft_limit_pct": (0, 100), "state_max_age_hours": (0, 24 * 30),
        "weekly_token_limit": (1, 10**13)}, nullable={"weekly_token_limit"})
    hist = raw.get("history")
    if isinstance(hist, dict) and "explore_down" in hist:
        hist = dict(hist)
        v = hist.pop("explore_down")
        if isinstance(v, bool):
            d["history"]["explore_down"] = v
        else:
            cfg.errors.append(f"history.explore_down: expected true or false, got {v!r}")
    _section(cfg, {"history": hist}, "history", {
        "min_samples": (1, 10_000), "fail_rate_up": (0, 1), "success_rate_down": (0, 1),
        "window_days": (1, 3650)})
    run = raw.get("run")
    if isinstance(run, dict):
        run = dict(run)
        for k in ("test_command", "permission_mode"):
            if k not in run:
                continue
            v = run.pop(k)
            if v is None or isinstance(v, str) and v.strip():
                d["run"][k] = v.strip() if isinstance(v, str) else None
            else:
                cfg.errors.append(f"run.{k}: expected a non-empty string or null, got {v!r}")
        _section(cfg, {"run": run}, "run", {
            "test_timeout_s": (1, 86_400), "max_escalations": (0, 4), "max_turns": (1, 10_000),
            "max_budget_usd": (0.001, 100_000), "timeout_s": (10, 86_400)},
            nullable={"max_budget_usd"})
        if isinstance(d["run"]["max_escalations"], float):
            d["run"]["max_escalations"] = int(d["run"]["max_escalations"])
    elif run is not None:
        cfg.errors.append("run: expected a mapping")
    llm = raw.get("llm")
    if isinstance(llm, dict):
        llm = dict(llm)
        m, e = llm.pop("model", None), llm.pop("effort", None)
        if m is not None:
            if _valid_model(m):
                d["llm"]["model"] = m
            else:
                cfg.errors.append(f"llm.model: unknown model {m!r}")
        if e is not None:
            if e in EFFORTS:
                d["llm"]["effort"] = e
            else:
                cfg.errors.append(f"llm.effort: must be one of {'|'.join(EFFORTS)}, got {e!r}")
        _section(cfg, {"llm": llm}, "llm", {"threshold": (0, 1), "timeout_s": (1, 600)})
    elif llm is not None:
        cfg.errors.append("llm: expected a mapping")

    routing = raw.get("routing")
    if isinstance(routing, dict):
        for k, v in routing.items():
            if k == "max_escalation":
                if _num(cfg, "routing.max_escalation", v, 0, 4):
                    d["routing"][k] = int(v)
            elif k == "ceiling_without_critical":
                if v in TIERS:
                    d["routing"][k] = v
                else:
                    cfg.errors.append(f"routing.{k}: expected T0..T4, got {v!r}")
            else:
                cfg.warnings.append(f"routing.{k}: unknown field ignored")
    elif routing is not None:
        cfg.errors.append("routing: expected a mapping")

    hooks = raw.get("hooks")
    if isinstance(hooks, dict):
        for k, v in hooks.items():
            if k == "watchdog_ms":
                if _num(cfg, "hooks.watchdog_ms", v, 100, 60_000):
                    d["hooks"][k] = int(v)
            elif k in DEFAULTS["hooks"]:
                if isinstance(v, bool):
                    d["hooks"][k] = v
                else:
                    cfg.errors.append(f"hooks.{k}: expected true or false, got {v!r}")
            else:
                cfg.warnings.append(f"hooks.{k}: unknown field ignored")
    elif hooks is not None:
        cfg.errors.append("hooks: expected a mapping")


def _section(cfg: Config, raw: dict, name: str, ranges: dict[str, tuple[float, float]],
             nullable: set[str] = frozenset()) -> None:
    sec = raw.get(name)
    if sec is None:
        return
    if not isinstance(sec, dict):
        cfg.errors.append(f"{name}: expected a mapping")
        return
    for k, v in sec.items():
        if k not in ranges:
            cfg.warnings.append(f"{name}.{k}: unknown field ignored")
        elif v is None and k in nullable:
            cfg.data[name][k] = None
        elif _num(cfg, f"{name}.{k}", v, *ranges[k]):
            cfg.data[name][k] = v


_MODEL_RANK = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 3}


def _check_monotonic(cfg: Config) -> None:
    prev = None
    for t in TIERS:
        spec = cfg.data["tiers"][t]
        m = next((r for a, r in _MODEL_RANK.items() if a in spec["model"]), None)
        cur = (m, EFFORTS.index(spec["effort"]))
        if prev is not None and m is not None and prev[0] is not None and cur < prev:
            cfg.warnings.append(f"tiers.{t} ({spec['model']}/{spec['effort']}) is cheaper than "
                                f"the tier below it; escalation may not help")
        prev = cur
