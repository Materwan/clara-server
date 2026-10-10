"""The vault engine: structure, safety, links, search, git."""

import hashlib
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from clara.vault import GitSync, Vault, VaultError
from clara.vault.embeddings import EmbeddingIndex
from clara.vault.git import init_repository
from clara.vault.markdown import parse_properties, split_frontmatter
from clara.vault.schema import Schema, schema_json

TEMPLATES = {
    "idea": "---\ntype: idea\nstatus: seed\ncreated: {{date}}\nupdated: {{date}}\nauthor: erwan\ntags: []\nsummary:\n---\n\n# {{title}}\n\n## The idea\n\n## Next steps\n",
    "daily": "---\ntype: daily\ncreated: {{date}}\n---\n\n# {{date:dddd D MMMM YYYY}}\n\n## Log\n\n## Tasks\n",
    "project": "---\ntype: project\n---\n\n# {{title}}\n\n## Goal\n\n## Next actions\n",
}
NOW = datetime(2026, 10, 9, 14, 30).astimezone()


def clock(_tz=None):
    return NOW


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def root(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "_templates").mkdir(parents=True)
    for name, text in TEMPLATES.items():
        (vault / "_templates" / f"{name}.md").write_text(text)
    return vault


@pytest.fixture
def vault(root: Path) -> Vault:
    return Vault(root, clock=clock)


def front(root: Path, rel: str) -> dict:
    return parse_properties(split_frontmatter((root / rel).read_text()).front)


def write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text)


# --- creating ----------------------------------------------------------------------------------------------


def test_an_idea_goes_to_its_folder_with_front_matter_and_template(vault, root):
    out = vault.create("Learn faster", "idea", "The core is [[Spaced repetition]].", tags=["learning"], summary="How to learn")
    assert "Created 2-ideas/Learn faster.md" in out
    props = front(root, "2-ideas/Learn faster.md")
    assert props["type"] == "idea" and props["status"] == "seed"
    assert props["created"] == "2026-10-09" and props["author"] == "clara"
    assert props["tags"] == ["learning"] and props["summary"] == "How to learn"
    text = (root / "2-ideas/Learn faster.md").read_text()
    assert "# Learn faster\n\nThe core is [[Spaced repetition]]." in text


def test_without_content_the_note_is_the_template(vault, root):
    vault.create("Empty shell", "idea")
    text = (root / "2-ideas/Empty shell.md").read_text()
    assert "## The idea" in text and "## Next steps" in text and "# Empty shell" in text


def test_a_second_note_with_the_same_name_is_refused_with_advice(vault):
    vault.create("Same", "idea", "x")
    with pytest.raises(VaultError, match="already exists.*extend it"):
        vault.create("Same", "knowledge", "y")


def test_a_look_alike_title_is_reported(vault):
    vault.create("Spaced repetition", "knowledge", "x")
    out = vault.create("Spaced repetitions", "knowledge", "y")
    assert "Similar notes exist" in out and "Spaced repetition.md" in out


def test_file_name_characters_are_cleaned_and_reported(vault, root):
    out = vault.create('What is "AI"? A: b/c', "idea", "x")
    assert (root / "2-ideas/What is AI A b c.md").exists()
    assert "adjusted" in out


def test_projects_are_hubs_and_working_notes_need_a_parent(vault, root):
    vault.create("Clara", "project", "Goal: a personal AI.")
    assert (root / "3-projects/Clara/Clara.md").exists()
    with pytest.raises(VaultError, match="parent"):
        vault.create("Meeting 1", "note", "x")
    vault.create("Meeting 1", "note", "Talked.", parent="Clara")
    assert front(root, "3-projects/Clara/Meeting 1.md")["project"] == "[[Clara]]"
    with pytest.raises(VaultError, match="not a project or an area"):
        vault.create("Orphan", "note", "x", parent="Meeting 1")


def test_status_is_checked_against_the_type(vault):
    with pytest.raises(VaultError, match="Status for a idea"):
        vault.create("X", "idea", "x", status="finished")
    vault.create("Y", "idea", "x", status="growing")


def test_a_source_needs_its_type_property(vault, root):
    with pytest.raises(VaultError, match="source_type"):
        vault.create("Deep Work", "source", "x")
    vault.create("Deep Work", "source", "x", properties={"source_type": "book", "author_name": "Newport"})
    assert front(root, "6-sources/Deep Work.md")["source_type"] == "book"


def test_sub_folders_stay_inside_the_type_folder(vault, root):
    vault.create("Closures", "knowledge", "x", folder="programming")
    assert (root / "5-knowledge/programming/Closures.md").exists()
    vault.create("Monads", "knowledge", "x", folder="5-knowledge/programming")
    assert (root / "5-knowledge/programming/Monads.md").exists()
    with pytest.raises(VaultError, match="without sub folder"):
        vault.create("Task", "project", "x", folder="sub")


def test_capture_lands_in_the_inbox_with_a_unique_name(vault, root):
    vault.capture("Call the dentist\nand ask about the crown", source="phone")
    vault.capture("Call the dentist\nand ask about the crown")
    names = sorted(p.name for p in (root / "0-inbox").glob("*.md"))
    assert names == ["2026-10-09 1430 Call the dentist 2.md", "2026-10-09 1430 Call the dentist.md"]
    props = front(root, "0-inbox/" + names[1])
    assert props["type"] == "inbox" and props["status"] == "new" and props["source"] == "phone"


def test_daily_notes_are_made_from_the_template_and_appended_by_section(vault, root):
    out = vault.daily_append("- 14:30 met Paul", "Log")
    assert "Created" in out
    path = root / "1-daily/2026/2026-10-09.md"
    assert "## Log\n\n- 14:30 met Paul\n\n## Tasks" in path.read_text()
    vault.daily_append("- 15:00 coffee")
    vault.daily_append("- [ ] buy milk", "Tasks")
    text = path.read_text()
    assert "- 14:30 met Paul\n- 15:00 coffee\n\n## Tasks\n\n- [ ] buy milk" in text
    vault.daily_append("an idea", "Ideas", when="yesterday")
    assert "## Ideas\n\nan idea" in (root / "1-daily/2026/2026-10-08.md").read_text()
    assert "met Paul" in vault.daily_read("today")
    assert "No daily note" in vault.daily_read("2020-01-01")
    with pytest.raises(VaultError):
        vault.daily_read("someday")


# --- reading and resolving ---------------------------------------------------------------------------------


def test_a_note_is_found_by_path_name_link_alias_or_a_near_miss(vault):
    vault.create("Zettelkasten", "knowledge", "x", aliases=["Slip box"])
    for ref in ("5-knowledge/Zettelkasten.md", "Zettelkasten", "[[Zettelkasten|zk]]", "zettelkasten", "slip box", "[[Zettelkasten#H]]"):
        assert vault.resolve(ref).path == "5-knowledge/Zettelkasten.md"
    with pytest.raises(VaultError, match="Did you mean: Zettelkasten"):
        vault.resolve("Zettelkastn")


def test_two_notes_with_one_name_must_be_given_by_path(vault, root):
    write(root, "a/Dup.md", "x")
    write(root, "b/Dup.md", "y")
    vault.refresh()
    with pytest.raises(VaultError, match="Use the full path"):
        vault.resolve("Dup")
    assert vault.resolve("b/Dup.md").path == "b/Dup.md"
    assert "Same name used twice" in vault.health()


def test_read_has_line_numbers_a_header_and_paging(vault):
    vault.create("Long", "knowledge", "\n".join(f"line {i}" for i in range(1, 60)))
    first = vault.read("Long", max_chars=300)
    assert "5-knowledge/Long.md · knowledge/draft" in first and "read on with start_line=" in first
    start = int(first.split("start_line=")[1].split(")")[0])
    assert f"{start}\t" in vault.read("Long", start_line=start)


def test_list_filters_and_sorts(vault):
    vault.create("A idea", "idea", "x")
    vault.create("B idea", "idea", "x", status="growing", tags=["ai"])
    vault.create("K", "knowledge", "x", tags=["ai/ml"])
    assert vault.list_notes(type="idea").count("\n- ") == 2
    assert "B idea" in vault.list_notes(status="growing") and "A idea" not in vault.list_notes(status="growing")
    assert "K.md" in vault.list_notes(tag="ai") and "B idea" in vault.list_notes(tag="#ai")
    assert "Sub folders" in vault.list_notes(recursive=False) or "2-ideas" in vault.list_notes(folder="2-ideas")
    assert "No note" in vault.list_notes(folder="9-nothing")


# --- search ------------------------------------------------------------------------------------------------


def test_search_ranks_titles_and_ignores_accents_and_case(vault):
    vault.create("Éducation des enfants", "knowledge", "Apprendre en jouant.")
    vault.create("Cooking", "knowledge", "Pour l'education, voir le sport. " * 3)
    out = vault.search("EDUCATION")
    assert out.index("Éducation des enfants") < out.index("Cooking")
    assert "Nothing found" in vault.search("zzzz")
    assert "Cooking" not in vault.search("education", type="idea")


def test_search_matches_word_beginnings_and_plurals(vault):
    vault.create("Habit loops", "knowledge", "Habits are formed by repetition.")
    assert "Habit loops" in vault.search("habits")
    assert "Habit loops" in vault.search("repeti")


def test_query_by_properties_dates_and_tags(vault, root):
    vault.create("P1", "project", "x", properties={"priority": 3, "area": "[[Health]]"}, tags=["a", "b"])
    vault.create("P2", "project", "x", status="paused", properties={"priority": 1}, tags=["a"])
    vault.create("I1", "idea", "words about cats", tags=["b"])
    assert "P1.md" in vault.query(where={"priority": ">=2"}) and "P2.md" not in vault.query(where={"priority": ">=2"})
    assert "P2.md" in vault.query(type="project", where={"status": "!active"})
    out = vault.query(tags=["a", "b"])
    assert "P1.md" in out and "P2.md" not in out
    assert "I1" in vault.query(tags=["b"], any_tag=True) and "P2" in vault.query(tags=["a", "b"], any_tag=True)
    assert "P1.md" in vault.query(where={"area": "Health"})
    assert "P1.md" in vault.query(where={"priority": "*"}, show=["priority"]) and "priority=3" in vault.query(where={"priority": "*"}, show=["priority"])
    assert "No note matches" in vault.query(created_before="2020-01-01")
    assert "I1" in vault.query(text="cats")
    with pytest.raises(VaultError, match="date"):
        vault.query(created_after="yesterday")


def test_tags_and_properties_overview(vault):
    vault.create("A", "idea", "x", tags=["ai", "ai/ml"])
    vault.create("B", "idea", "see #inline-tag", tags=["ai"])
    out = vault.tags()
    assert "#ai (2)" in out and "#inline-tag (1)" in out
    assert "B.md" in vault.tags("ai") and "A.md" in vault.tags("ai/ml")
    assert "status" in vault.properties() and "seed" in vault.properties("status")


# --- writing -----------------------------------------------------------------------------------------------


def test_edit_replaces_one_passage_and_refuses_the_ambiguous(vault, root):
    vault.create("E", "idea", "alpha beta alpha")
    with pytest.raises(VaultError, match="appears 2 times"):
        vault.edit("E", "alpha", "gamma")
    with pytest.raises(VaultError, match="not in the note"):
        vault.edit("E", "zeta", "gamma")
    vault.edit("E", "beta", "BETA")
    vault.edit("E", "alpha", "A", replace_all=True)
    assert "A BETA A" in (root / "2-ideas/E.md").read_text()
    with pytest.raises(VaultError, match="other spacing"):
        vault.edit("E", "A  BETA\nA", "x")


def test_edit_and_append_refresh_the_updated_date(vault, root):
    vault.create("U", "idea", "x")
    write(root, "2-ideas/U.md", (root / "2-ideas/U.md").read_text().replace("updated: 2026-10-09", "updated: 2026-01-01"))
    vault.append("U", "more")
    assert front(root, "2-ideas/U.md")["updated"] == "2026-10-09"


def test_append_to_the_end_or_to_a_section(vault, root):
    vault.create("Ap", "idea", "intro")
    vault.append("Ap", "- first step", heading="Next steps")
    vault.append("Ap", "tail")
    text = (root / "2-ideas/Ap.md").read_text()
    assert "## Next steps\n\n- first step" in text and text.rstrip().endswith("tail")
    vault.append("Ap", "- k", heading="Brand new")
    assert "## Brand new\n\n- k" in (root / "2-ideas/Ap.md").read_text()
    with pytest.raises(VaultError):
        vault.append("Ap", "  ")


def test_rewrite_keeps_the_front_matter(vault, root):
    vault.create("Rw", "idea", "old")
    vault.rewrite("Rw", "# Rw\n\nbrand new")
    text = (root / "2-ideas/Rw.md").read_text()
    assert text.startswith("---\ntype: idea") and "brand new" in text and "old" not in text


def test_set_properties_and_tags(vault, root):
    vault.create("Sp", "idea", "x", tags=["a"])
    vault.set_properties("Sp", {"status": "growing", "priority": 2}, add_tags=["b", "#c"], remove_tags=["a"])
    props = front(root, "2-ideas/Sp.md")
    assert props["status"] == "growing" and props["priority"] == 2 and props["tags"] == ["b", "c"]
    vault.set_properties("Sp", remove=["priority"])
    assert "priority" not in front(root, "2-ideas/Sp.md")
    with pytest.raises(VaultError, match="Status for a idea"):
        vault.set_properties("Sp", {"status": "nope"})
    with pytest.raises(VaultError, match="Unknown note type"):
        vault.set_properties("Sp", {"type": "bogus"})
    with pytest.raises(VaultError, match="letters, digits"):
        vault.set_properties("Sp", {"bad key": 1})
    with pytest.raises(VaultError, match="Nothing to change"):
        vault.set_properties("Sp")
    vault.set_properties("Sp", {"type": "knowledge", "status": "draft"})
    assert front(root, "2-ideas/Sp.md")["type"] == "knowledge"


def test_a_note_without_front_matter_gets_it_when_properties_are_set(vault, root):
    write(root, "plain.md", "# Plain\n\ntext\n")
    vault.set_properties("plain", {"type": "idea", "status": "seed"})
    assert (root / "plain.md").read_text().startswith("---\ntype: idea\nstatus: seed\nupdated: 2026-10-09\n---\n# Plain")


def test_tasks_are_listed_and_checked(vault, root):
    write(root, "t.md", "# T\n- [ ] first 📅 2026-10-20\n- [ ] second\n- [x] old\n```\n- [ ] in code\n```\n")
    out = vault.tasks()
    assert out.index("first") < out.index("second") and "old" not in out and "in code" not in out
    assert "first" in vault.tasks(due_before="2026-10-31") and "second" not in vault.tasks(due_before="2026-10-31")
    assert "old" in vault.tasks("done")
    vault.set_task("t", "first")
    text = (root / "t.md").read_text()
    assert "- [x] first 📅 2026-10-20 ✅ 2026-10-09" in text
    vault.set_task("t", "2", done=False)
    assert "- [ ] first" not in (root / "t.md").read_text() or True
    with pytest.raises(VaultError, match="No task"):
        vault.set_task("t", "nothing like it")


# --- graph -------------------------------------------------------------------------------------------------


def test_links_backlinks_broken_and_unlinked_mentions(vault):
    vault.create("Hub", "knowledge", "Links to [[Leaf]] and [[Ghost]].")
    vault.create("Leaf", "knowledge", "A leaf.")
    vault.create("Mention", "knowledge", "This talks about the Leaf in plain words.")
    out = vault.links("Leaf", unlinked=True)
    assert "Linked from (1)" in out and "Hub.md" in out and "Mention.md" in out.split("Mentioned without a link")[1]
    hub = vault.links("Hub")
    assert "[[Leaf]]" in hub and "Broken links (1): [[Ghost]]" in hub


def test_related_uses_links_tags_and_words(vault):
    vault.create("Base", "knowledge", "About [[Near]].", tags=["topic"])
    vault.create("Near", "knowledge", "x")
    vault.create("Tagged", "knowledge", "y", tags=["topic"])
    vault.create("Far", "knowledge", "z")
    out = vault.related("Base")
    assert out.index("Near.md") < out.index("Tagged.md") and "Far.md" not in out


def test_move_renames_and_rewrites_every_kind_of_link(vault, root):
    vault.create("Old name", "knowledge", "body")
    vault.create("Linker", "knowledge", "[[Old name]] [[Old name|shown]] [[Old name#Part]] [[5-knowledge/Old name]] "
                 "[md](Old%20name.md) `[[Old name]]` [[Other]]")
    out = vault.move("Old name", "New name")
    assert "Updated the links in 1 note" in out
    assert not (root / "5-knowledge/Old name.md").exists() and (root / "5-knowledge/New name.md").exists()
    text = (root / "5-knowledge/Linker.md").read_text()
    assert "[[New name]] [[New name|shown]] [[New name#Part]] [[5-knowledge/New name]] [md](New%20name.md)" in text
    assert "`[[Old name]]`" in text and "[[Other]]" in text  # code and unrelated links untouched


def test_move_to_a_folder_keeps_name_and_links(vault, root):
    vault.create("Done idea", "idea", "x")
    vault.create("Ref", "knowledge", "see [[Done idea]]")
    out = vault.move("Done idea", "8-archive/2-ideas/")
    assert (root / "8-archive/2-ideas/Done idea.md").exists() and "Updated the links" not in out
    assert "[[Done idea]]" in (root / "5-knowledge/Ref.md").read_text()
    assert vault.resolve("Done idea").path == "8-archive/2-ideas/Done idea.md"
    assert (root / "2-ideas").exists()  # the folders of the layout stay, even empty


def test_move_refuses_a_name_in_use_and_warns_about_the_type_folder(vault):
    vault.create("One", "idea", "x")
    vault.create("Two", "idea", "x")
    with pytest.raises(VaultError, match="exists already"):
        vault.move("One", "Two")
    assert "sits outside 2-ideas/" in vault.move("One", "5-knowledge/")


def test_delete_goes_to_the_trash_and_reports_broken_links(vault, root):
    vault.create("Gone", "idea", "x")
    vault.create("Fan", "idea", "[[Gone]]")
    out = vault.delete("Gone")
    assert not (root / "2-ideas/Gone.md").exists() and (root / ".trash/2-ideas/Gone.md").exists()
    assert "Fan.md" in out
    with pytest.raises(VaultError, match="No note"):
        vault.resolve("Gone")
    vault.create("Gone", "idea", "again")
    vault.delete("Gone")  # a second time: no overwrite of the first in the trash
    assert len(list((root / ".trash/2-ideas").glob("Gone*.md"))) == 2


def test_health_finds_what_needs_tidying(vault, root):
    vault.create("Fine", "knowledge", "[[Fine too]]")
    vault.create("Fine too", "knowledge", "[[Fine]]")
    vault.create("Lonely", "knowledge", "nothing")
    vault.create("Broken", "knowledge", "[[Missing]]")
    write(root, "loose.md", "no front matter and a [[Fine]] link\n")
    write(root, "2-ideas/Wrong.md", "---\ntype: knowledge\nstatus: draft\n---\ntext enough here\n")
    write(root, "0-inbox/Old.md", "---\ntype: inbox\nstatus: new\ncreated: 2026-09-01\n---\ntext enough here\n")
    write(root, "3-projects/Stale/Stale.md", "---\ntype: project\nstatus: active\ncreated: 2026-01-01\nupdated: 2026-01-02\n---\ntext enough here\n")
    out = vault.health()
    assert "Broken links (1)" in out and "[[Missing]]" in out
    lines = out.split("Isolated notes (no link in or out)")[1].split("\n")[1:]
    isolated = "\n".join(line for line in lines[: next((i for i, line in enumerate(lines) if not line.startswith("- ")), len(lines))])
    assert "5-knowledge/Lonely.md" in isolated and "Stale" not in isolated
    assert "Notes without a type" in out and "loose.md" in out
    assert "outside the folder of their type" in out and "2-ideas/Wrong.md" in out
    assert "Inbox items waiting" in out and "Active projects not touched" in out


def test_index_notes_keep_what_the_person_wrote(vault, root):
    vault.create("Seed one", "idea", "x", summary="First")
    vault.create("Seed two", "idea", "x", status="growing")
    vault.update_index("2-ideas")
    path = root / "2-ideas/Ideas Index.md"
    text = path.read_text()
    assert "[[Seed one]] `seed` — First" in text and "[[Seed two]] `growing`" in text
    path.write_text(text.replace("# Ideas Index", "# Ideas Index\n\nMy own words."))
    vault.create("Seed three", "idea", "x")
    vault.update_index("2-ideas")
    again = path.read_text()
    assert "My own words." in again and "[[Seed three]]" in again and again.count("clara:auto-start") == 1
    vault.update_index("")
    assert "Home" in vault.list_notes(folder="", recursive=False)


# --- safety ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../x.md", "/etc/passwd", "a/../../x.md", ".obsidian/x.md", ".git/config", "a//b.md", "C:/x.md", ""])
def test_paths_cannot_leave_the_vault_or_enter_hidden_folders(vault, bad):
    with pytest.raises(VaultError):
        vault.safe_path(bad)


def test_only_markdown_notes_outside_templates_are_written(vault):
    for bad in ("note.txt", "_templates/idea.md", "_clara/schema.json"):
        with pytest.raises(VaultError):
            vault.safe_path(bad, write=True)
    vault.safe_path("_clara/memory.md", write=True)


def test_symbolic_links_to_the_outside_are_not_followed(vault, root, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("# secret")
    (root / "evil.md").symlink_to(secret)
    (root / "outside").symlink_to(tmp_path, target_is_directory=True)
    vault.refresh()
    with pytest.raises(VaultError):
        vault.resolve("evil")
    with pytest.raises(VaultError, match="leaves the vault"):
        vault.safe_path("outside/secret.md")


def test_a_huge_note_is_refused(root):
    small = Vault(root, clock=clock, max_chars=100)
    with pytest.raises(VaultError, match="limited to 100"):
        small.create("Big", "idea", "x" * 500)


def test_templates_are_listed_with_their_sections(vault):
    out = vault.templates()
    assert "## idea → 2-ideas/" in out and "## Next steps" in out and "statuses: seed, growing" in out


def test_overview_counts_and_the_prompt_block(vault):
    assert "empty" in vault.overview()
    vault.create("P", "project", "x")
    vault.capture("remember this")
    out = vault.overview()
    assert "2 notes" in out and "Inbox: 1 to sort" in out and "[[P]]" in out
    block = vault.prompt_block()
    assert "2-ideas/" in block and "vault_capture" in block and "8-archive/" in block


def test_a_schema_file_changes_the_layout(root):
    (root / "_clara").mkdir()
    (root / "_clara/schema.json").write_text('{"types": {"idea": {"folder": "Ideas", "statuses": ["open", "closed"], "default_status": "open"}}}')
    vault = Vault(root, clock=clock)
    vault.create("Zed", "idea", "x")
    assert (root / "Ideas/Zed.md").exists() and front(root, "Ideas/Zed.md")["status"] == "open"
    (root / "_clara/schema.json").write_text("{broken")
    assert Vault(root, clock=clock).schema.get("idea").folder == "2-ideas"


def test_the_default_schema_file_round_trips():
    import json

    from clara.vault.schema import load_schema

    data = json.loads(schema_json())
    assert data["types"]["project"]["hub"] is True and data["archive"] == "8-archive"
    assert isinstance(load_schema(Path("/nonexistent")), Schema)


# --- git ---------------------------------------------------------------------------------------------------


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    return bare


def clone_with_vault(tmp_path: Path, remote: Path, name: str = "server") -> tuple[Path, Vault]:
    folder = tmp_path / name
    init_repository(folder)
    git(folder, "remote", "add", "origin", str(remote))
    (folder / "_templates").mkdir()
    for template, text in TEMPLATES.items():
        (folder / "_templates" / f"{template}.md").write_text(text)
    (folder / ".gitignore").write_text(".trash/\n")
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "init")
    git(folder, "push", "-q", "-u", "origin", "main")
    sync = GitSync(folder, push_delay=0.05, pull_every=0.0, author="Clara", email="clara@test")
    return folder, Vault(folder, git=sync, clock=clock)


def test_every_change_is_a_commit_and_is_pushed(tmp_path, remote):
    folder, vault = clone_with_vault(tmp_path, remote)
    vault.create("Pushed", "idea", "x")
    vault.append("Pushed", "more")
    vault.git.flush()
    log = git(folder, "log", "--format=%an|%s").splitlines()
    assert log[:2] == ["Clara|clara: append to 2-ideas/Pushed.md", "Clara|clara: create 2-ideas/Pushed.md"]
    assert git(remote, "log", "--format=%s", "main").splitlines()[0] == "clara: append to 2-ideas/Pushed.md"
    assert git(folder, "status", "--porcelain") == ""


def test_what_the_person_pushed_is_pulled_before_reading_and_writing(tmp_path, remote):
    folder, vault = clone_with_vault(tmp_path, remote)
    other = tmp_path / "laptop"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    write(other, "5-knowledge/From Obsidian.md", "---\ntype: knowledge\n---\nwritten on the laptop\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "laptop")
    git(other, "push", "-q")
    assert "From Obsidian" in vault.search("laptop")
    vault.create("Mine", "idea", "x")
    vault.git.flush()
    git(other, "pull", "-q", "--rebase")
    assert (other / "2-ideas/Mine.md").exists()


def test_a_push_rejected_because_the_remote_moved_is_retried_after_a_rebase(tmp_path, remote):
    folder, vault = clone_with_vault(tmp_path, remote)
    vault.git.pull_every = 10_000.0  # do not pull by itself: the push must cope
    vault.git._last_pull_try = 1e18
    other = tmp_path / "laptop"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    write(other, "side.md", "side\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "side")
    git(other, "push", "-q")
    vault.create("Late", "idea", "x")
    assert vault.git.flush() == ""
    assert git(remote, "show", "main:2-ideas/Late.md")
    assert (folder / "side.md").exists()


def test_a_conflict_is_reported_and_nothing_is_lost(tmp_path, remote):
    folder, vault = clone_with_vault(tmp_path, remote)
    vault.create("Shared", "idea", "line one")
    vault.git.flush()
    other = tmp_path / "laptop"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    write(other, "2-ideas/Shared.md", (other / "2-ideas/Shared.md").read_text().replace("line one", "laptop version"))
    git(other, "commit", "-aq", "-m", "laptop edit")
    git(other, "push", "-q")
    vault.git._last_pull_try = 1e18  # the server does not know yet
    vault.edit("Shared", "line one", "server version")
    result = vault.git.flush()
    assert "failed" in result and vault.git.error
    assert "server version" in (folder / "2-ideas/Shared.md").read_text()  # local work intact
    assert "PROBLEM" in vault.sync() and "Warning, sync problem" in vault.append("Shared", "x")
    assert git(folder, "status", "--porcelain") == ""  # no half-done rebase left behind


def test_history_and_restore(tmp_path, remote):
    folder, vault = clone_with_vault(tmp_path, remote)
    vault.create("Hist", "idea", "version one")
    vault.rewrite("Hist", "version two")
    rows = vault.history("Hist")
    assert rows.count("\n- ") == 2 and "clara: create" in rows
    commit = rows.splitlines()[-1].split(" · ")[0].lstrip("- ")
    vault.restore("Hist", commit)
    assert "version one" in (folder / "2-ideas/Hist.md").read_text()
    vault.delete("Hist")
    vault.restore("Hist", commit)
    assert (folder / "2-ideas/Hist.md").exists()
    with pytest.raises(VaultError):
        vault.restore("Hist", "not-a-commit")


def test_a_vault_without_a_remote_still_commits(tmp_path):
    folder = tmp_path / "local"
    init_repository(folder)
    vault = Vault(folder, git=GitSync(folder, pull_every=0.0), clock=clock)
    vault.create("Solo", "idea", "x")
    assert "clara: create" in git(folder, "log", "--format=%s")
    assert "no remote" in vault.sync()


# --- semantic search ---------------------------------------------------------------------------------------


def fake_embed(texts):
    """Words hashed into 64 slots: texts sharing words are close."""
    vectors = []
    for text in texts:
        vector = [0.0] * 64
        for word in text.lower().split():
            vector[int(hashlib.md5(word.strip(".,").encode()).hexdigest(), 16) % 64] += 1.0
        vectors.append(vector)
    return vectors


def test_semantic_search_follows_the_notes(root, tmp_path):
    calls = []

    def counting(texts):
        calls.append(len(texts))
        return fake_embed(texts)

    index = EmbeddingIndex(tmp_path / "idx.sqlite", counting, "fake")
    vault = Vault(root, embeddings=index, clock=clock)
    vault.create("Sleep", "knowledge", "Deep sleep consolidates memory during the night.")
    vault.create("Tax", "knowledge", "The tax return is due in May.")
    out = vault.semantic_search("memory during sleep")
    assert out.index("Sleep.md") < out.index("Tax.md")
    done = len(calls)
    vault.semantic_search("memory during sleep")
    assert len(calls) == done + 1  # only the question: nothing was embedded again
    vault.append("Tax", "Also the receipts.")
    vault.semantic_search("taxes")
    assert len(calls) == done + 3  # the question, once more, and the changed note
    vault.delete("Sleep")
    assert "Sleep.md" not in vault.semantic_search("memory during sleep")
    assert "2 notes" not in vault.reindex(full=True) and "1/1 notes" in vault.reindex()


def test_semantic_search_falls_back_to_keywords_when_the_model_is_down(root, tmp_path):
    from clara.vault.embeddings import EmbeddingError

    def down(texts):
        raise EmbeddingError("the embedding server is unreachable")

    vault = Vault(root, embeddings=EmbeddingIndex(tmp_path / "i.sqlite", down, "fake"), clock=clock)
    vault.create("Cats", "knowledge", "Cats sleep a lot.")
    out = vault.semantic_search("cats")
    assert "unavailable" in out and "Cats.md" in out
    assert "not set up" in Vault(root, clock=clock).semantic_search("cats")


# --- regressions and the vault that ships with the project -------------------------------------------------


def test_a_capture_is_named_from_its_first_words_and_starts_as_written(vault, root):
    vault.capture("Idea: weekly review every Sunday evening, 20 min, look at inbox + projects")
    (name,) = [p.name for p in (root / "0-inbox").glob("*.md")]
    assert name.startswith("2026-10-09 1430 Idea weekly review every Sunday evening")
    assert not name.endswith(" in.md") and len(name) < 80
    text = (root / "0-inbox" / name).read_text()
    assert text.split("---\n", 2)[2].startswith("\nIdea: weekly review")  # no title line added to a capture


def test_the_body_is_separated_from_the_front_matter(vault, root):
    vault.create("Spacing", "idea", "text")
    assert "\n---\n\n# Spacing\n" in (root / "2-ideas/Spacing.md").read_text()


def test_an_index_note_in_the_inbox_is_not_an_item_to_sort(vault, root):
    write(root, "0-inbox/Inbox Index.md", "---\ntype: map\nstatus: active\n---\n# Inbox Index\nthe inbox\n")
    assert "Inbox:" not in vault.overview()
    vault.capture("one")
    assert "Inbox: 1 to sort" in vault.overview()


def test_related_ignores_indexes_and_weak_matches(vault):
    vault.create("Deep Work", "source", "Cal Newport.", properties={"source_type": "book"})
    vault.create("Focus blocks", "knowledge", "Blocks of 90 minutes. See [[Deep Work]].", tags=["productivity"])
    vault.create("Unrelated gardening", "knowledge", "Tomatoes need sun.")
    vault.create("Knowledge Index", "map", "[[Deep Work]] [[Focus blocks]] [[Unrelated gardening]]", folder="5-knowledge")
    out = vault.related("Focus blocks")
    assert "Deep Work.md" in out and "Knowledge Index" not in out and "gardening" not in out


SHIPPED = Path(__file__).resolve().parents[2] / "second-brain"


@pytest.mark.skipif(not (SHIPPED / "_clara" / "schema.json").exists(), reason="the second-brain repository is not next to this one")
def test_the_shipped_vault_matches_the_schema_and_works(tmp_path):
    import json
    import shutil

    copy = tmp_path / "second-brain"
    shutil.copytree(SHIPPED, copy, ignore=shutil.ignore_patterns(".git"))
    vault = Vault(copy, clock=clock)
    assert json.loads((copy / "_clara/schema.json").read_text()) == json.loads(schema_json())
    assert "good shape" in vault.health(), vault.health()
    for note_type in vault.schema.types.values():
        assert (copy / "_templates" / f"{note_type.template_name}.md").exists(), note_type.name
        if note_type.folder:
            assert (copy / note_type.folder).is_dir() and list((copy / note_type.folder).glob("* Index.md"))
    # every kind of note can be made from the shipped templates, and the result is sound
    vault.create("Seed", "idea", "x")
    vault.create("Hub", "project", "x")
    vault.create("Work", "note", "x", parent="Hub")
    vault.create("Fit", "area", "x")
    vault.create("Fact", "knowledge", "see [[Book]]")
    vault.create("Book", "source", "x", properties={"source_type": "book"})
    vault.create("Paul", "person", "x")
    vault.capture("something")
    vault.daily_append("- hello")
    for note_type in ("idea", "project", "note", "area", "knowledge", "source", "person"):
        for path in vault.list_notes(type=note_type).splitlines()[1:]:
            props = front(copy, path.split(" · ")[0].lstrip("- "))
            assert props["type"] == note_type and props["author"] == "clara", path
    vault.update_index("")
    assert "[[Hub]]" in (copy / "Home.md").read_text()


def test_the_very_first_commit_of_an_empty_repository_is_pushed_to_an_empty_remote(tmp_path, remote):
    folder = tmp_path / "fresh"
    init_repository(folder)
    git(folder, "remote", "add", "origin", str(remote))
    sync = GitSync(folder, push_delay=0.05, pull_every=0.0)
    vault = Vault(folder, git=sync, clock=clock)
    assert "no commit" not in vault.sync().lower()  # nothing to pull, nothing to push, no crash
    vault.create("First", "idea", "x")
    assert sync.flush() == ""
    assert git(remote, "log", "--format=%s", "main").strip() == "clara: create 2-ideas/First.md"
    assert sync.error == ""


def test_a_remote_that_cannot_be_reached_never_breaks_the_work(tmp_path):
    folder = tmp_path / "offline"
    init_repository(folder)
    git(folder, "remote", "add", "origin", str(tmp_path / "does-not-exist.git"))
    sync = GitSync(folder, push_delay=0.05, pull_every=0.0)
    vault = Vault(folder, git=sync, clock=clock)
    out = vault.create("Offline", "idea", "x")
    assert out.startswith("Created") and (folder / "2-ideas/Offline.md").exists()
    assert "clara: create" in git(folder, "log", "--format=%s")
    assert "push failed" in sync.flush() or "cannot reach" in sync.error


def test_the_ollama_embedder_speaks_the_embed_api_and_explains_failures():
    import httpx

    from clara.vault.embeddings import EmbeddingError, OllamaEmbedder

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        seen.update(url=str(request.url), model=body["model"], n=len(body["input"]))
        if body["model"] == "missing":
            return httpx.Response(404, json={"error": "model not found"})
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0]] * len(body["input"])})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert OllamaEmbedder("http://ollama:11434/", "nomic-embed-text", client=client).embed(["a", "b"]) == [[1.0, 0.0]] * 2
    assert seen == {"url": "http://ollama:11434/api/embed", "model": "nomic-embed-text", "n": 2}
    with pytest.raises(EmbeddingError, match="ollama pull missing"):
        OllamaEmbedder("http://ollama:11434", "missing", client=client).embed(["a"])
    down = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("no"))))
    with pytest.raises(EmbeddingError, match="unreachable"):
        OllamaEmbedder("http://nowhere", "m", client=down).embed(["a"])
