"""The eval harness itself (API-free): the keyword baseline planner drives the real graph + backend through
every scenario, and the grader must catch a planner that lies about having done something."""
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
import run_eval  # noqa: E402

from skydesk import faults, tools  # noqa: E402
from skydesk.agent.verify import verify_reply  # noqa: E402

SPEC = yaml.safe_load((ROOT / "eval" / "scenarios.yaml").read_text())


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "OUT", tmp_path)
    monkeypatch.setenv("SKYDESK_DB", str(tmp_path / "unused.db"))
    tools.set_search_backend(lambda q: [{"doc_id": "faq", "title": "FAQ", "text": q}])
    yield tmp_path
    faults.set_injector(None)
    tools.set_search_backend(None)


@pytest.mark.parametrize("sid", list(SPEC))
def test_baseline_planner_passes_every_scenario_with_zero_unsafe_writes(harness, sid):
    r = run_eval.run_scenario(sid, SPEC[sid], "rules", harness / "arl.jsonl", "t")
    assert r["unsafe_writes"] == 0
    assert r["passed"], r["fails"]


def test_grader_catches_a_false_success_claim(harness, monkeypatch):
    import rules_planner

    class Liar(rules_planner.RulesPlanner):
        def step(self, messages):
            return self.say("Your refund of $500 has been processed.")

    monkeypatch.setattr(rules_planner, "RulesPlanner", Liar)
    r = run_eval.run_scenario("s01_24h_refund", SPEC["s01_24h_refund"], "rules", harness / "arl.jsonl", "t")
    assert not r["passed"] and not r["db_correct"]
    assert all("$500" not in (t["reply"] or "") for t in r["turns"])  # verify.py never lets it through


def test_latest_policy_check_decides_eligibility_claims():
    first = {"tool": "check_eligibility", "ok": True, "data": {"eligible": True, "conditions": []}}
    later = {"tool": "check_eligibility", "ok": True, "data": {"eligible": False, "conditions": []}}
    assert verify_reply("It is not eligible any more.", [first, later])["action"] == "pass"
    assert verify_reply("It is not eligible any more.", [later, first])["action"] != "pass"
