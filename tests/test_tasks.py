import csv
import io
import json

import pytest

from h2lab import tasks


def serialize(spec, obj):
    if spec.fmt == "json":
        return json.dumps(obj)
    if spec.fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(obj[0])
        w.writerows(obj[1])
        return buf.getvalue()
    return obj


@pytest.mark.parametrize("task", tasks.all_tasks(), ids=lambda t: t.id)
def test_reference_solution_scores_100_and_each_mistake_costs(task):
    spec = task.sim
    assert task.grade({spec.deliverable: serialize(spec, spec.solve(task.files, set()))}).score == 100
    for req in [r for r in spec.reqs if "." in r]:
        g = task.grade({spec.deliverable: serialize(spec, spec.solve(task.files, {req}))})
        assert g.score < 100, f"{task.id}: mistake {req} is invisible to the grader"
    assert task.grade({}).score == 0


def test_public_view_hides_truth_and_checks():
    for t in tasks.all_tasks():
        pub = t.public()
        assert "truth" not in pub and "checks" not in pub
        assert all(c.description not in json.dumps(pub) for c in t.checks if len(c.description) > 24)


def test_nine_tasks_in_three_domains():
    assert len(tasks.all_tasks()) == 9
    assert {t.domain for t in tasks.all_tasks()} == {"finance", "ops", "analytics"}
