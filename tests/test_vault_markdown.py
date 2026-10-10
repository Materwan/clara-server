"""The Obsidian text layer of the vault: front matter, links, tags, tasks, sections, templates."""

from datetime import datetime

from clara.vault.markdown import (
    add_to_section,
    apply_properties,
    fill_template,
    find_headings,
    find_links,
    find_tags,
    find_tasks,
    first_paragraph,
    format_date,
    mask_code,
    parse_properties,
    section_range,
    split_frontmatter,
    tags_of,
)

NOTE = """---
type: idea
status: seed
created: 2026-10-01
tags:
  - ai
  - "second brain"
aliases: [Brain, "Mind, extended"]
rating: 4
done: false
project: "[[Clara]]"
empty:
# a comment
summary: "He said: \\"hi\\""
---

# Title

Body text.
"""


def test_the_front_matter_is_split_from_the_body():
    parts = split_frontmatter(NOTE)
    assert parts.front.startswith("type: idea")
    assert parts.body.lstrip().startswith("# Title")
    assert parts.body_line == 16  # the line after the closing ---
    assert split_frontmatter("no front\n---\nx").front is None
    assert split_frontmatter("---\nnever closed\n").front is None


def test_properties_are_read_with_their_types():
    props = parse_properties(split_frontmatter(NOTE).front)
    assert props["type"] == "idea"
    assert props["created"] == "2026-10-01"
    assert props["tags"] == ["ai", "second brain"]
    assert props["aliases"] == ["Brain", "Mind, extended"]
    assert props["rating"] == 4 and props["done"] is False
    assert props["project"] == "[[Clara]]"
    assert props["empty"] is None
    assert props["summary"] == 'He said: "hi"'


def test_unquoted_wikilink_and_inline_comment():
    props = parse_properties("project: [[Clara]]\nstatus: active # not done\nurl: http://a.b/#x")
    assert props["project"] == "[[Clara]]"
    assert props["status"] == "active"
    assert props["url"] == "http://a.b/#x"


def test_tags_can_be_a_text():
    assert tags_of({"tags": "a, #b c"}) == ["a", "b", "c"]
    assert tags_of({}) == []


def test_changing_a_property_keeps_every_other_line():
    changed = apply_properties(NOTE, {"status": "growing", "priority": 2}, ["rating"])
    front = split_frontmatter(changed).front
    assert "status: growing" in front and "priority: 2" in front
    assert "rating" not in front
    assert "# a comment" in front and 'aliases: [Brain, "Mind, extended"]' in front
    assert front.index("status: growing") < front.index("tags:")  # in place, not moved
    assert changed.endswith("Body text.\n")


def test_lists_and_special_values_are_written_safely():
    text = apply_properties("body\n", {"tags": ["a", "b c"], "project": "[[X]]", "when": "2026-10-10", "n": "12", "t": "a: b"})
    props = parse_properties(split_frontmatter(text).front)
    assert props == {"tags": ["a", "b c"], "project": "[[X]]", "when": "2026-10-10", "n": "12", "t": "a: b"}
    assert text.endswith("---\nbody\n")


def test_removing_everything_removes_the_front_matter():
    assert apply_properties("---\na: 1\n---\nbody\n", None, ["a"]) == "body\n"


def test_code_is_not_scanned():
    text = "see [[Real]] and `[[Inline]]`\n```\n[[Fenced]] #nottag\n- [ ] no\n```\n#tag [[Other|alias]]"
    names = [link.target for link in find_links(text)]
    assert names == ["Real", "Other"]
    assert find_tags(text) == ["tag"]
    assert find_tasks(text) == []
    assert len(mask_code(text)) == len(text)


def test_links_have_anchor_alias_embed_and_line():
    links = find_links("a\n[[Note#Part|shown]] ![[pic.png]] [text](other%20note.md#h) [web](https://x.y)\n")
    assert [(x.target, x.anchor, x.alias, x.embed, x.line, x.markdown) for x in links] == [
        ("Note", "#Part", "shown", False, 2, False),
        ("pic.png", "", "", True, 2, False),
        ("other%20note.md", "#h", "text", False, 2, True),
    ]


def test_tags_ignore_headings_urls_and_numbers():
    text = "# Heading\nText #ai/ml and #2026 and http://x.y/#frag and C#. [[Note#Head]] #été"
    assert find_tags(text) == ["ai/ml", "été"]


def test_tasks_with_due_dates():
    tasks = find_tasks("- [ ] write 📅 2026-10-31\n  * [x] done\n- [-] cancelled\nplain")
    assert [(t.line, t.done, t.due) for t in tasks] == [(1, False, "2026-10-31"), (2, True, ""), (3, False, "")]


def test_headings_and_first_paragraph():
    body = "# T\n\n> quote\n\nFirst real line.\n\n## Sub\n"
    assert [(h.level, h.text) for h in find_headings(body)] == [(1, "T"), (2, "Sub")]
    assert first_paragraph(body) == "First real line."


def test_adding_to_a_section():
    text = "# T\n\n## Log\n\n- a\n\n## Tasks\n\n- [ ] x\n"
    out = add_to_section(text, "log", "- b")
    assert out == "# T\n\n## Log\n\n- a\n- b\n\n## Tasks\n\n- [ ] x\n"
    out = add_to_section(text, "Notes", "- n")
    assert out.endswith("## Notes\n\n- n\n")
    assert section_range(text, "## Tasks")[2] == 2
    assert section_range(text, "Nope") is None


def test_template_variables_and_dates():
    moment = datetime(2026, 10, 9, 14, 5)
    assert format_date(moment, "dddd D MMMM YYYY [at] HH:mm") == "Friday 9 October 2026 at 14:05"
    out = fill_template("# {{title}} {{date}} {{time}} {{date:YY/MM}} {{content}}", "X", moment, "body")
    assert out == "# X 2026-10-09 14:05 26/10 body"
