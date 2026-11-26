"""Question sets are strict, versioned and immutable once published."""

from __future__ import annotations

import json
import shutil

import pytest

from modules.waitlist.questions import (
    CONTENT_ROOT,
    QuestionBank,
    QuestionSet,
    QuestionSetError,
    digest,
)

# The published bytes. Editing a published file must fail here AND at
# startup; a new version is a new file, never an edit.
PUBLISHED = {
    "questions/v1.json": (
        "475f55fe8ed1086c500a40bc313c8a556c49a0eda34d5cca72683435eaac8d6a"
    ),
    "questions/v2.json": (
        "37d653261d6bf594f2657c240c0448e56f65b0319d8547311ca186486eee2bb5"
    ),
    "consent/v1.json": (
        "0a72a7f09ee53fcba8a3cdb295af71076c9e96bdd082036a81d5f3dc0d5775a2"
    ),
}


@pytest.fixture
def content(tmp_path):
    target = tmp_path / "waitlist"
    shutil.copytree(CONTENT_ROOT, target)
    return target


def test_published_versions_are_never_edited():
    for name, expected in PUBLISHED.items():
        assert digest(CONTENT_ROOT / name) == expected, f"{name} was edited"
    lock = json.loads((CONTENT_ROOT / "published.lock.json").read_text())["files"]
    assert {name: lock[name] for name in PUBLISHED} == PUBLISHED


def test_v2_asks_what_other_is_on_role_and_location_and_v1_is_unchanged():
    bank = QuestionBank.load()
    v1, v2 = bank.sets[1], bank.sets[2]
    assert bank.active is v2
    assert not v1.find("role").allow_other
    assert not v1.find("company_location").allow_other
    for question in ("role", "company_location"):
        assert v2.find(question).allow_other
        assert v2.find(question).other_max_length == 60
    # Nothing else differs: same ids, types, prompts and options.
    strip = lambda s: [  # noqa: E731
        q.model_dump(exclude={"allow_other", "other_max_length"}) for q in s.questions
    ]
    assert strip(v1) == strip(v2)


def test_v2_is_the_approved_set():
    bank = QuestionBank.load()
    active = bank.active
    assert active.version == 2 and len(active.questions) == 6
    assert [q.id for q in active.questions] == [
        "company_location",
        "company_size",
        "role",
        "waiting_on",
        "ai_today",
        "tools",
    ]
    labels = {q.id: [o.label for o in q.options or ()] for q in active.questions}
    assert labels["company_size"] == [
        "1\u201310",
        "11\u201350",
        "51\u2013200",
        "201\u20131000",
        "1000+",
    ]
    assert "Microsoft 365" in labels["tools"] and len(labels["tools"]) == 11
    assert active.find("tools").allow_other and active.find("waiting_on").allow_other
    assert (
        active.find("role").allow_other and active.find("company_location").allow_other
    )
    assert bank.active_consent.text == (
        "By joining you agree that Weaves may store your email and answers to "
        "decide which teams we work with first."
    )


def test_editing_a_published_file_stops_startup(content):
    path = content / "questions/v1.json"
    path.write_text(path.read_text().replace("Where is", "Where's"))
    with pytest.raises(QuestionSetError, match="was edited"):
        QuestionBank.load(content)


def test_an_unpinned_or_missing_file_stops_startup(content):
    (content / "questions/v3.json").write_text(
        (content / "questions/v1.json").read_text()
    )
    with pytest.raises(QuestionSetError, match="does not match"):
        QuestionBank.load(content)
    (content / "questions/v3.json").unlink()
    (content / "consent/v1.json").unlink()
    with pytest.raises(QuestionSetError):
        QuestionBank.load(content)


def _relock(content):
    lock = {
        "files": {
            f"{folder}/{p.name}": digest(p)
            for folder in ("questions", "consent")
            for p in (content / folder).glob("*.json")
        }
    }
    (content / "published.lock.json").write_text(json.dumps(lock))


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["questions"].extend(d["questions"][:1]),  # duplicate id
        lambda d: d["questions"].extend(
            [{**d["questions"][0], "id": f"extra_{i}"} for i in range(1, 3)]
        ),  # seven questions
        lambda d: d["questions"][0].update(optional=False),
        lambda d: d["questions"][0].update(type="free_form"),
        lambda d: d["questions"][0].update(surprise=1),
        lambda d: d["questions"][0].update(allow_other=True),
        lambda d: d["questions"][3].update(other_max_length=None),
        lambda d: d["questions"][0].update(max_length=280),
        lambda d: d["questions"][0].update(options=d["questions"][0]["options"][:1]),
        lambda d: d["questions"][0]["options"].append(d["questions"][0]["options"][0]),
        lambda d: d.update(version=3),
        lambda d: d.update(questions=[]),
    ],
)
def test_invalid_sets_are_refused_at_startup(content, change):
    path = content / "questions/v1.json"
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))
    _relock(content)
    with pytest.raises(QuestionSetError):
        QuestionBank.load(content)


def test_a_new_version_is_a_new_file_and_becomes_active(content):
    data = json.loads((content / "questions/v2.json").read_text())
    data["version"] = 3
    (content / "questions/v3.json").write_text(json.dumps(data))
    lock = json.loads((content / "published.lock.json").read_text())
    lock["files"]["questions/v3.json"] = digest(content / "questions/v3.json")
    (content / "published.lock.json").write_text(json.dumps(lock))
    bank = QuestionBank.load(content)
    assert bank.active.version == 3 and bank.question_set(2) is not None
    assert isinstance(bank.question_set(2), QuestionSet)
