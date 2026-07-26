import os, tempfile
from sessions.zh.s08_skills import Skill, SkillLoader, Issue, IssueStatus


def _tmp_skill(content):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "SKILL.md")
    with open(path, "w") as f:
        f.write(content)
    return d, path


def test_match_selects_skill():
    d, _ = _tmp_skill("""---
name: code-review
description: review code
keywords: review, 审查
---
Focus on security.""")
    loader = SkillLoader(d)
    issue = Issue(id="i1", workspace_id="ws", title="review the auth module")
    skill = loader.select(issue)
    assert skill is not None
    assert skill.name == "code-review"
    assert "Focus on security" in skill.body


def test_no_match_field_not_selected():
    d, _ = _tmp_skill("""---
name: general
description: general skill
---
No match field.""")
    loader = SkillLoader(d)
    issue = Issue(id="i1", workspace_id="ws", title="anything")
    assert loader.select(issue) is None


def test_no_match_runs_without_skill():
    d, _ = _tmp_skill("""---
name: code-review
keywords: review
---
Focus on security.""")
    loader = SkillLoader(d)
    issue = Issue(id="i1", workspace_id="ws", title="fix login bug")
    assert loader.select(issue) is None  # 不命中


def test_first_match_wins():
    d = tempfile.mkdtemp()
    for name, match in [("first", "fix"), ("second", "bug")]:
        path = os.path.join(d, "SKILL.md" if name == "first" else "SKILL2.md")
        with open(path, "w") as f:
            f.write(f"---\nname: {name}\nmatch: {match}\n---\nbody")
    # 注意:SkillLoader 扫 **/SKILL.md,SKILL2.md 不被扫到
    # 改用子目录:
    # 实际测试用子目录
    d2 = tempfile.mkdtemp()
    os.makedirs(os.path.join(d2, "a"), exist_ok=True)
    os.makedirs(os.path.join(d2, "b"), exist_ok=True)
    with open(os.path.join(d2, "a", "SKILL.md"), "w") as f:
        f.write("---\nname: first\nkeywords: fix\n---\nbody a")
    with open(os.path.join(d2, "b", "SKILL.md"), "w") as f:
        f.write("---\nname: second\nkeywords: fix.*bug\n---\nbody b")
    loader = SkillLoader(d2)
    issue = Issue(id="i1", workspace_id="ws", title="fix login bug")
    skill = loader.select(issue)
    assert skill.name == "first"  # sorted glob → a/ 先于 b/


def test_empty_dir_no_skills():
    d = tempfile.mkdtemp()
    loader = SkillLoader(d)
    assert loader.list() == []
    assert loader.select(Issue(id="i", workspace_id="ws", title="anything")) is None


def test_skill_body_is_markdown_content():
    d, _ = _tmp_skill("""---
name: deploy
description: deploy guide
keywords: deploy
---
## Deploy Steps
1. Build
2. Push
3. Release
""")
    loader = SkillLoader(d)
    issue = Issue(id="i1", workspace_id="ws", title="deploy the service")
    skill = loader.select(issue)
    assert "## Deploy Steps" in skill.body
    assert "1. Build" in skill.body
