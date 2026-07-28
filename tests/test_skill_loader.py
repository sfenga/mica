import os, tempfile
from sessions.zh.s08_skills import SkillLoader


def _tmp_skill(content, subdir=""):
    d = tempfile.mkdtemp()
    root = os.path.join(d, subdir) if subdir else d
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, "SKILL.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return d, path


def test_loads_skill_from_markdown():
    d, path = _tmp_skill("""---
name: code-review
description: review code
---
Focus on security.""")
    loader = SkillLoader(d)
    skills = loader.list()
    assert len(skills) == 1
    assert skills[0].name == "code-review"
    assert skills[0].description == "review code"
    assert skills[0].path == path
    assert "Focus on security" in skills[0].body


def test_get_skill_by_name_case_insensitive():
    d, _ = _tmp_skill("""---
name: deploy
description: deploy guide
---
## Deploy Steps
1. Build
2. Push
3. Release
""")
    loader = SkillLoader(d)
    assert loader.get("deploy").name == "deploy"
    assert loader.get("DEPLOY").name == "deploy"
    assert loader.get(" missing ") is None


def test_scans_nested_skill_directories_in_order():
    d = tempfile.mkdtemp()
    for subdir, name in [("b", "second"), ("a", "first")]:
        os.makedirs(os.path.join(d, subdir), exist_ok=True)
        with open(os.path.join(d, subdir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(f"---\nname: {name}\ndescription: d\n---\nbody")
    loader = SkillLoader(d)
    assert [s.name for s in loader.list()] == ["first", "second"]


def test_ignores_files_without_frontmatter():
    d, _ = _tmp_skill("No frontmatter here.")
    loader = SkillLoader(d)
    assert loader.list() == []
    assert loader.get("anything") is None


def test_empty_dir_no_skills():
    d = tempfile.mkdtemp()
    loader = SkillLoader(d)
    assert loader.list() == []
    assert loader.get("anything") is None
