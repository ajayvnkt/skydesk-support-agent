"""Fill the {{PLACEHOLDERS}} in video/narration.json with spoken-word numbers -> video/narration.filled.json,
and write the grading card's figures (digits) to video/out/grading.json.

Sources: eval/results/openai-final2_*.json (final run, 2 passes), eval/results/openai-before_*.json (first live
run), the policy test files (collected with pytest), eval/agent_report_card_policy.md (Week 1 Agent Report Card
with the project policy) and store/cost_ledger.json (every model call's usd).

    python video/fill_numbers.py        # run with the project venv (.venv/bin/python) so pytest is importable
"""
import json
import math
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
        "seventeen eighteen nineteen").split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
POLICY_TESTS = ["tests/test_policy.py", "tests/test_policy_corpus.py"]
RECORDING_MARGIN = 0.03   # the recording itself adds ~$0.02 of planner calls after this script runs


def words(n: int) -> str:
    """Integer 0..999999 -> words, no hyphens (TTS-friendly)."""
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else " " + ONES[n % 10])
    if n < 1000:
        return ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + words(n % 100))
    return words(n // 1000) + " thousand" + ("" if n % 1000 == 0 else " " + words(n % 1000))


def latest(tag: str) -> dict:
    files = sorted((ROOT / "eval/results").glob(f"openai-{tag}_2*.json"))
    assert files, tag
    return json.loads(files[-1].read_text())


def numbers() -> tuple[dict, dict]:
    final, first = latest("final2"), latest("before")
    s1, s2 = final["summaries"]
    f = first["summaries"][0]
    n = len(final["passes"][0])
    assert f["passed"] < s1["passed"], (f["passed"], s1["passed"])
    unsafe = s1["unsafe_writes"] + s2["unsafe_writes"]
    pol = min(s1["policy_correctness"], s2["policy_correctness"])
    rec = min(s1["recovery_rate"], s2["recovery_rate"])
    n_fault = 3   # s16, s17, s18 (docs/EVAL_REPORT.md section 1)
    lo, hi = sorted((s1["passed"], s2["passed"]))
    cpt = max(s1["cost_per_task_usd"], s2["cost_per_task_usd"])
    p50 = (s1["latency_turn_p50_s"] + s2["latency_turn_p50_s"]) / 2

    out = subprocess.run([sys.executable, "-m", "pytest", "-q", *POLICY_TESTS], cwd=ROOT, capture_output=True, text=True)
    m = re.search(r"(\d+) passed", out.stdout)
    assert out.returncode == 0 and m and "failed" not in out.stdout, out.stdout[-400:]
    n_tests = int(m[1])

    arc = (ROOT / "eval/agent_report_card_policy.md").read_text()
    crit = int(re.search(r"Runs with a critical finding: \*\*(\d+)\*\*", arc)[1])

    ledger = json.loads((ROOT / "store/cost_ledger.json").read_text())
    total = sum(x["usd"] for x in ledger)
    bound = math.ceil((total + RECORDING_MARGIN) * 10) * 10   # cents, rounded up to the next ten
    assert bound <= 100, f"ledger total ${total:.4f} is too close to the one dollar cap"

    spoken = {
        "SCENARIOS": words(n),
        "FIRST_RUN": f"{words(f['passed'])} of {words(n)}",
        "FINAL_PASSES": (f"{words(lo)} of {words(n)}" if lo == hi else f"{words(lo)} and {words(hi)} of {words(n)}"),
        "UNSAFE": words(unsafe).capitalize(),
        "POLICY_CORRECT": f"{words(round(pol * 100))} percent",
        "RECOVERY": f"{words(round(rec * n_fault))} of {words(n_fault)}",
        "POLICY_TESTS": words(n_tests),
        "COST_TASK": "about a third of a cent" if 0.0028 <= cpt <= 0.0038 else f"about {words(round(cpt * 1000))} tenths of a cent",
        "LATENCY_P50": f"{words(round(p50))} seconds",
        "ARC_CRIT": words(crit),
        "TOTAL_SPEND": f"under {words(bound)} cents",
    }
    card = {
        "scenarios": n, "first_run": f"{f['passed']}/{n}", "final": f"{s1['passed']}/{n} · {s2['passed']}/{n}",
        "unsafe": unsafe, "writes": s1["writes"] + s2["writes"], "policy_correct": f"{round(pol * 100)}%",
        "recovery": f"{round(rec * n_fault)}/{n_fault}", "policy_tests": n_tests, "cost_task": f"${cpt:.4f}",
        "p50": f"{p50:.1f} s", "arc_crit": crit, "total_usd": f"${total:.2f}",
        "source": f"eval/results/openai-final2 ({final['tag']}) · first run: openai-before · docs/EVAL_REPORT.md",
    }
    return spoken, card


def main() -> int:
    assert words(56) == "fifty six" and words(235) == "two hundred thirty five"
    spoken, card = numbers()
    for k, v in spoken.items():
        print(f"{k:<15} {v}")
    text = (HERE / "narration.json").read_text(encoding="utf-8")
    filled = re.sub(r"\{\{(\w+)\}\}", lambda mt: spoken[mt[1]], text)
    left = re.findall(r"\{\{\w+\}\}", filled)
    if left:
        print("unfilled placeholders:", left)
        return 1
    scenes = json.loads(filled)
    long = [(b["key"], len(b["say"].split())) for s in scenes for b in s["beats"] if len(b["say"].split()) > 26]
    digits = [(b["key"], b["say"]) for s in scenes for b in s["beats"] if re.search(r"\d", b["say"])]
    total = sum(len(b["say"].split()) for s in scenes for b in s["beats"])
    (HERE / "narration.filled.json").write_text(filled, encoding="utf-8")
    (HERE / "out").mkdir(exist_ok=True)
    (HERE / "out/grading.json").write_text(json.dumps(card, indent=1), encoding="utf-8")
    print(f"wrote video/narration.filled.json: {total} words" + (f"; over 26 words: {long}" if long else "")
          + (f"; digits left: {digits}" if digits else ""))
    return 1 if long or digits else 0


if __name__ == "__main__":
    sys.exit(main())
