"""The numbers of a results database as JSON (scores, single-phase stats, answer checks, token totals): run with LLM_PROBE_HOME
pointing at a home whose results.db is to be examined. The GUI regression compares the output for the sample database with
baseline_sample.json, so a change of how scores are computed shows up as a diff (update the baseline on purpose)."""
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from llm_probe import storage  # noqa: E402

db = storage.connect()
out = {}
for r in db.execute("select * from runs order by id").fetchall():
    sc = storage.run_scores(db, r)
    ss = [asdict(s) for s in storage.single_stats(db, r["id"])]
    ck = {pid: [(rep, c.correct, c.total, c.undecided, c.cut) for rep, c in cs] for pid, cs in storage.run_checks(db, r["id"]).items()}
    out[r["id"]] = {"scores": {k: v for k, v in sc.items() if k != "overflow"}, "single": ss, "checks": ck,
                    "tok": storage.token_usage(db, r["id"])["total"]}
print(json.dumps(out, sort_keys=True, default=str))
