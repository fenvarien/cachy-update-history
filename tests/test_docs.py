from pathlib import Path
import re
import unittest
from urllib.parse import unquote


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LINK_PATTERN = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
HEADING_PATTERN = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
HEADING_LEVEL_PATTERN = re.compile(r"^(#{1,6})\s+", re.MULTILINE)
FENCED_BLOCK_PATTERN = re.compile(r"```.*?```", re.DOTALL)


def _anchor(heading):
    heading = re.sub(r"[`*_]", "", heading).casefold()
    heading = re.sub(r"[^\w\- ]", "", heading)
    return re.sub(r"-+", "-", heading.replace(" ", "-")).strip("-")


class DocumentationTests(unittest.TestCase):
    def test_internal_markdown_links_and_anchors_exist(self):
        failures = []
        markdown_files = sorted({
            *PROJECT_ROOT.glob("*.md"),
            *(PROJECT_ROOT / "docs").rglob("*.md"),
            *(PROJECT_ROOT / ".github").rglob("*.md"),
        })

        for document in markdown_files:
            text = document.read_text(encoding="utf-8")
            prose = FENCED_BLOCK_PATTERN.sub("", text)
            for raw_target in LINK_PATTERN.findall(prose):
                target = raw_target.strip().split(maxsplit=1)[0]
                if target.startswith(("https://", "http://", "mailto:")):
                    continue

                path_text, separator, fragment = target.partition("#")
                target_path = (
                    document if not path_text
                    else (document.parent / unquote(path_text)).resolve()
                )
                if not target_path.exists():
                    failures.append(
                        f"{document.relative_to(PROJECT_ROOT)} -> {target} (missing file)"
                    )
                    continue

                if separator and target_path.suffix.casefold() == ".md":
                    target_text = target_path.read_text(encoding="utf-8")
                    target_text = FENCED_BLOCK_PATTERN.sub("", target_text)
                    anchors = {_anchor(value)
                               for value in HEADING_PATTERN.findall(target_text)}
                    if unquote(fragment).casefold() not in anchors:
                        failures.append(
                            f"{document.relative_to(PROJECT_ROOT)} -> {target} "
                            "(missing heading)"
                        )

        self.assertTrue(markdown_files, "No Markdown documentation found")
        self.assertEqual(failures, [], "\n" + "\n".join(failures))

    def test_readme_indexes_maintainer_documents(self):
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        for target in (
            "docs/USER_GUIDE.md",
            "docs/ARCHITECTURE.md",
            "CHANGELOG.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
        ):
            with self.subTest(target=target):
                self.assertIn(f"]({target})", readme)

    def test_primary_documents_have_consistent_heading_structure(self):
        documents = sorted({
            *PROJECT_ROOT.glob("*.md"),
            *(PROJECT_ROOT / "docs").rglob("*.md"),
        })
        for document in documents:
            text = document.read_text(encoding="utf-8")
            prose = FENCED_BLOCK_PATTERN.sub("", text)
            levels = [len(value)
                      for value in HEADING_LEVEL_PATTERN.findall(prose)]
            with self.subTest(document=document.relative_to(PROJECT_ROOT)):
                self.assertTrue(levels)
                self.assertEqual(levels[0], 1)
                self.assertEqual(levels.count(1), 1)
                self.assertTrue(all(current <= previous + 1
                                    for previous, current in zip(levels, levels[1:])))
                self.assertEqual(text.count("```") % 2, 0)

    def test_changelog_has_single_unreleased_section(self):
        changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertEqual(changelog.count("## [Unreleased]"), 1)
        self.assertIn("[Unreleased]:", changelog)


if __name__ == "__main__":
    unittest.main()
