"""Public CM1 skill bundle discovery, metadata, contracts and CLI regressions."""
from __future__ import annotations

import ast
import importlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages/qwenpaw-data-skills"
SKILLS = PACKAGE / "skills"
REQUIRED = (
    "routing/data-intent-router/SKILL.md", "planning/analysis-plan-builder/SKILL.md",
    "domains/cm-skills/SKILL.md", "domains/cm-skills/references/mcp-tools.md",
    "domains/cm-skills/references/neo4j.md", "domains/cm-skills/references/holoage.md",
    "workflows/fetch-data/SKILL.md", "workflows/fetch-data/scripts/generate_nl2sql_prompt.py",
    "atomic/bi-report-generation/SKILL.md", "atomic/bi-report-generation/scripts/report_blocks.py",
    "atomic/bi-report-generation/scripts/report_builder.py", "atomic/bi-report-generation/resources/report.css",
    "atomic/report-format-extractor/SKILL.md", "atomic/report-schedule-builder/SKILL.md",
    "workflows/report-reproduction/SKILL.md", "workflows/report-reproduction/references/artifact-contract.md",
    "workflows/report-reproduction/references/section-lifecycle.md",
    "workflows/report-reproduction/scripts/report_cli.py",
    "workflows/report-reproduction/scripts/gold_parser/section_splitter.py",
)


@pytest.mark.parametrize("relative", REQUIRED)
def test_required_bundle_assets_exist(relative):
    path = SKILLS / relative
    assert path.is_file() and path.stat().st_size > 0


def test_public_namespace_and_canonical_app_compatible_assets(monkeypatch):
    monkeypatch.syspath_prepend(str(PACKAGE / "src"))
    bundle = importlib.import_module("qwenpaw_data.skills")
    assert bundle.__version__ == "0.4.0"
    assert bundle.bundle_root() == SKILLS
    assert bundle.get_skill_path("cm-skills") == SKILLS / "domains/cm-skills"
    assert len(bundle.iter_skill_paths()) >= 30
    with pytest.raises(ValueError):
        bundle.get_skill_path("../cm-skills")
    with pytest.raises(KeyError):
        bundle.get_skill_path("nonexistent-skill")
    project = tomllib.loads((PACKAGE / "pyproject.toml").read_text())
    assert project["project"]["version"] == "0.4.0"
    assert project["project"]["name"] == "qwenpaw-data-skills"
    assert project["project"]["license"] == "Apache-2.0"
    includes = project["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert includes["skills"] == "qwenpaw_data_skills/skills"
    assert includes["../../LICENSE"] == "LICENSE"
    assert includes["../../NOTICE"] == "NOTICE"


@pytest.mark.parametrize("path", sorted(SKILLS.rglob("SKILL.md")), ids=lambda p: p.parent.name)
def test_skill_frontmatter_name_and_local_markdown_links(path):
    text = path.read_text(encoding="utf-8")
    frontmatter = re.match(r"\A---\n(.*?)\n---", text, re.S)
    assert frontmatter, path
    assert re.search(r"^name: " + re.escape(path.parent.name) + r"$", frontmatter[1], re.M)
    assert re.search(r"^description: .+", frontmatter[1], re.M)
    assert len(text.splitlines()) < 500, path
    for link in re.findall(r"\]\(([^)]+)\)", text):
        if "://" not in link and not link.startswith(("#", "<")):
            assert (path.parent / link.split("#")[0]).is_file(), (path, link)


def test_public_asset_hygiene_and_no_excluded_harness():
    private = re.compile("|".join((
        r"alibaba-inc\.com", r"gitlab\.alibaba", r"hub\.docker\.alibaba", "/home/" + "admin",
        r"\bAuthX\b", r"\bODPS_(?:BUC|INTERNAL)\b", r"\bqwen" + r"chat\b", r"\bbai" + r"lian\b",
        r"\btong" + r"yi\b", r"\bdamo_\w+", "odps_execution_project", r"\bdatapaw[._-]",
    )), re.I)
    excluded = {"dab_tools", "data_agent_bench", "data-agent-bench", "rsi", "task_mining", "question_generation"}
    files = list(SKILLS.rglob("*"))
    assert files
    for path in files:
        assert not (set(path.parts) & excluded), path
        if path.is_file() and path.suffix in {".md", ".py", ".html", ".css", ".json", ".js"}:
            assert not private.search(path.read_text(encoding="utf-8")), path


def test_all_python_assets_parse():
    paths = list(SKILLS.rglob("*.py"))
    assert len(paths) >= 20
    for path in paths:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_nl2sql_context_cli_preserves_complete_caliber(tmp_path):
    problem = tmp_path / "problem.json"
    problem.write_text(json.dumps({"question": "Compute net GAAP revenue by region."}))
    context = tmp_path / "context.json"
    context.write_text(json.dumps({"metrics": {"net GAAP": {"desc": "Verified revenue definition", "src": "Graph",
        "calibers": [{"table": "revenue", "expr": "SUM(amount) FILTER (WHERE state = 'paid')", "window": "day", "additivity": "additive", "default": True}]}},
        "tables": {"revenue": {"desc": "Revenue fact table", "cols": {"amount": ["DECIMAL", "Revenue amount"], "state": ["STRING", "Payment state", ["paid", "pending"]]}}}}))
    command = [sys.executable, str(SKILLS / "workflows/fetch-data/scripts/generate_nl2sql_prompt.py"),
               "--problem", str(problem), "--context", str(context)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "SUM(amount) FILTER (WHERE state = 'paid')" in result.stdout
    assert "Revenue fact table" in result.stdout and "Payment state" in result.stdout
    help_result = subprocess.run([command[0], command[1], "--help"], capture_output=True, text=True)
    assert "--context" in help_result.stdout and "--metrics" not in help_result.stdout


@pytest.mark.parametrize("relative,function", [
    ("atomic/bi-conversion-rate/scripts/compute_conversion.py", "compute_conversion"),
    ("atomic/bi-retention-rate/scripts/compute_retention.py", "compute_retention"),
])
def test_retained_rate_calculations(relative, function):
    import importlib.util
    pd = pytest.importorskip("pandas")
    spec = importlib.util.spec_from_file_location(function, SKILLS / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = pd.DataFrame({"begin": [100, 200], "end": [40, 100]})
    assert getattr(module, function)(data, "begin", "end").tolist() == [.4, .5]


def test_retained_distribution_statistics():
    import importlib.util
    pd = pytest.importorskip("pandas")
    spec = importlib.util.spec_from_file_location("distribution_stats", SKILLS / "atomic/bi-distribution-analysis/scripts/distribution_stats.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stats = module.compute_basic_stats(pd.DataFrame({"value": [1, 2, 3]}), "value")
    assert stats["均值"] == [2.0] and stats["中位数"] == [2.0]


def test_normal_creation_evolution_and_confidence_are_retained():
    algo = ROOT / "packages/qwenpaw-data-host-core/src/qwenpaw_data/host/core/algo"
    tools = (algo / "skill_evolution/tools.py").read_text()
    for marker in ("skill_view", "skill_propose_patch", "skill_evolution_done"):
        assert marker in tools
    assert (algo / "skill_create/manager.py").is_file()
    assert "confidence" in (algo / "evaluation/scorer.py").read_text()
    assert "GAAP" in (SKILLS / "planning/analysis-plan-builder/references/metrics/tob.md").read_text()


def test_new_host_plan_guidance_is_not_old_plan_runtime():
    for guide in ("planning/analysis-plan-builder/SKILL.md", "runtime/interaction-strategy/SKILL.md"):
        text = (SKILLS / guide).read_text()
        assert "PlanCreate" in text and "ask_user_question" in text
        assert not re.search(r"create_plan|update_subtask|revise_current_plan", text)
