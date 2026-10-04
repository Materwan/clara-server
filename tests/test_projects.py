"""Projects: reading what people upload, storing it, and what the model sees of it."""

import io
import zipfile

import pytest

from clara.ingest import IngestError, docx_text, expand, extract, ignored, unpack
from clara.projects import PROJECT_TOOLS, ProjectError, Projects


def make_zip(files: dict[str, bytes | str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def make_docx(*paragraphs: str, table: list[list[str]] | None = None) -> bytes:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    if table:
        rows = "".join(
            "<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row) + "</w:tr>"
            for row in table
        )
        body += f"<w:tbl>{rows}</w:tbl>"
    return make_zip({"word/document.xml": f'<?xml version="1.0"?><w:document {w}><w:body>{body}</w:body></w:document>'})


# --- ingest ----------------------------------------------------------------------------------------------


def test_text_and_code_are_kept_with_their_language():
    file = extract("src\\main.py", "﻿print('hé')\n".encode())
    assert (file.path, file.kind, file.text) == ("src/main.py", "python", "print('hé')\n")
    assert extract("notes.txt", "caf\xe9".encode("cp1252")).text == "café"  # not UTF-8: Windows-1252


@pytest.mark.parametrize(("path", "data", "reason"), [
    ("a.bin", b"\x00\x01\x02", "not a text file"),
    ("logo.png", b"PNGish", "not a text file"),
    ("empty.md", b"  \n", "empty"),
    ("../etc/passwd", b"root", "may not go up"),
])
def test_what_cannot_be_used_says_why(path, data, reason):
    with pytest.raises(IngestError, match=reason):
        extract(path, data)


def test_word_documents_are_read_without_a_dependency():
    data = make_docx("Title", "Second paragraph", table=[["a", "b"], ["1", "2"]])
    assert docx_text(data) == "Title\nSecond paragraph\n| a | b |\n| 1 | 2 |"
    assert extract("report.docx", data).kind == "docx"
    with pytest.raises(IngestError, match="Word"):
        docx_text(b"not a zip")


def test_a_zip_keeps_its_text_files_and_leaves_out_the_rest():
    data = make_zip({
        "repo-abc/README.md": "# Hi",
        "repo-abc/src/app.js": "let a = 1;",
        "repo-abc/node_modules/x/index.js": "junk",
        "repo-abc/package-lock.json": "{}",
        "repo-abc/img/logo.png": b"\x89PNG",
        "repo-abc/inner.zip": make_zip({"a.txt": "a"}),
    })
    files, skipped = unpack(data, "proj", strip_root=True)
    assert sorted(f.path for f in files) == ["proj/README.md", "proj/src/app.js"]
    reasons = {s.path: s.reason for s in skipped}
    assert reasons["proj/node_modules/x/index.js"] == "in node_modules/"
    assert reasons["proj/package-lock.json"] == "generated file"
    assert reasons["proj/img/logo.png"] == "not a text file"
    assert reasons["proj/inner.zip"] == "an archive inside the archive"


def test_an_uploaded_zip_goes_in_a_folder_named_after_it():
    files, skipped = expand("site.zip", make_zip({"index.html": "<p>", "css/a.css": "p{}"}))
    assert sorted(f.path for f in files) == ["site/css/a.css", "site/index.html"]
    assert skipped == []
    files, skipped = expand("broken.zip", b"nope")
    assert files == [] and "not a .zip" in skipped[0].reason


def test_ignored_folders_and_files():
    assert ignored("a/.git/config") == "in .git/"
    assert ignored("pkg.egg-info/PKG-INFO") == "in pkg.egg-info/"
    assert ignored("dist.py") is None and ignored("src/build.gradle") is None


# --- the store -------------------------------------------------------------------------------------------


@pytest.fixture
def projects(memory):
    return Projects(memory, max_bytes=1_000, max_files=5, inline_percent=40)


@pytest.fixture
def erwan(memory):
    return memory.resolve("web", "erwan", "Erwan")


def files(*pairs):
    from clara.ingest import ExtractedFile

    return [ExtractedFile(path, "python" if path.endswith(".py") else "", text) for path, text in pairs]


def test_projects_are_made_listed_changed_and_deleted(projects, erwan, memory):
    project = projects.create(erwan.id, "  My   app ", "A web app", "Answer in French")
    assert (project.name, project.description, project.instructions, project.files) == ("My app", "A web app", "Answer in French", 0)
    other = projects.create(memory.resolve("web", "zoe").id, "Zoe's")
    assert [p.id for p in projects.of(erwan.id)] == [project.id]
    assert projects.update(project.id, instructions="Answer in English").instructions == "Answer in English"
    with pytest.raises(ProjectError, match="name"):
        projects.create(erwan.id, "   ")
    memory.add_turn("web:erwan:1", erwan.id, "hi", [], project_id=project.id)
    assert projects.get(project.id).conversations == 1
    assert projects.delete(project.id) == 1
    assert projects.get(project.id) is None and projects.get(other.id) is not None
    assert memory.conversation_info("web:erwan:1").project_id is None  # the conversation stays, out of the project


def test_files_are_added_replaced_and_limited(projects, erwan):
    project = projects.create(erwan.id, "p")
    result = projects.add(project.id, files(("a.py", "x" * 400), ("b.py", "y" * 400), ("c.py", "z" * 400)))
    assert result.added == ["a.py", "b.py"]
    assert "full" in result.skipped[0].reason and result.skipped[0].path == "c.py"
    result = projects.add(project.id, files(("a.py", "short")))
    assert result.replaced == ["a.py"] and projects.totals(project.id) == (2, 405)
    projects.add(project.id, files(("d/1.py", "1"), ("d/2.py", "2"), ("d/e/3.py", "3")))
    assert projects.add(project.id, files(("f.py", "f"))).skipped[0].reason.startswith("the project is full (5 files")
    assert projects.remove(project.id, "d", folder=True) == 3
    assert projects.remove(project.id, "a.py") == 1
    assert [f.path for f in projects.files(project.id)] == ["b.py"]


def test_small_projects_are_put_whole_in_the_prompt(projects, erwan):
    project = projects.create(erwan.id, "Notes", "My notes", "Be brief")
    projects.add(project.id, files(("todo.md", "- buy ```milk```"), ("main.py", "print(1)")))
    context = projects.context(project.id, window=10_000)
    assert context.inline
    assert "## Project: Notes" in context.text and "My notes" in context.text and "Be brief" in context.text
    assert '<document path="main.py" type="python">\n```python\nprint(1)\n```\n</document>' in context.text
    assert "````\n- buy ```milk```\n````" in context.text  # a longer fence than any inside
    assert context.text.index("main.py") < context.text.index("todo.md")  # always the same order: cacheable


def test_big_projects_are_listed_for_the_tools(projects, erwan):
    project = projects.create(erwan.id, "Big")
    projects.add(project.id, files(("src/a.py", "a" * 400), ("src/b.py", "def target():\n    pass\n")))
    context = projects.context(project.id, window=200)  # 40% of 200 tokens: far too small
    assert not context.inline
    assert "read_project_file" in context.text and "- src/a.py (400 chars)" in context.text
    assert "a" * 400 not in context.text


def test_the_tools_list_read_and_search(projects, erwan):
    project = projects.create(erwan.id, "Tools")
    projects.add(project.id, files(("src/a.py", "import os\n\ndef Target():\n    return 1\n"), ("doc/b.md", "target practice\nÉté")))
    assert projects.list_paths(project.id, "src") == "src/a.py (38 chars)"
    assert projects.read(project.id, "src/a.py", 3, 4) == "src/a.py, lines 3-4 of 4:\n    3  def Target():\n    4      return 1"
    assert "No file" in projects.read(project.id, "nope.py")
    found = projects.search(project.id, "target")
    assert found.startswith("Matches in 2 files:") and "src/a.py:3: def Target():" in found and "doc/b.md:1:" in found
    assert projects.search(project.id, "target", folder="doc").count("\n") == 1
    assert "doc/b.md:2: Été" in projects.search(project.id, "été")  # not ASCII: the case is still ignored
    assert "src/a.py:3" in projects.search(project.id, r"def \w+\(", regex=True)
    with pytest.raises(ValueError, match="regular expression"):
        projects.search(project.id, "(", regex=True)


def test_a_person_who_is_erased_loses_their_projects(projects, erwan, memory):
    project = projects.create(erwan.id, "Mine")
    projects.add(project.id, files(("a.py", "a")))
    memory.delete_person(erwan.id)
    assert projects.get(project.id) is None
    assert memory.database.execute("SELECT COUNT(*) FROM project_files").fetchone()[0] == 0


def test_project_tools_exist_in_the_toolbox():
    from clara.tools import default_toolbox

    assert PROJECT_TOOLS <= default_toolbox().names
