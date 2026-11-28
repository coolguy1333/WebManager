"""The Docs page: task-first topics, and every link into it lands on one."""

import re
import unittest
from pathlib import Path

from tests import test_app as base

ROOT = Path(__file__).resolve().parent.parent


class DocsPageTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user

    def setUp(self):
        self.setUp_base()
        self.app.config["APPS_ENABLED"] = True
        self.login_user(self.add_user("alice", "alice@example.com"))
        self.page = self.client.get("/docs").get_data(as_text=True)

    def test_it_starts_with_how_to_put_something_online(self):
        first = re.search(r'data-doc-topic[^>]*>', self.page).group(0)
        self.assertIn("open", first)
        self.assertLess(self.page.index("Put a website or app online"), self.page.index("Make your project an app"))
        self.assertIn('href="/new"', self.page)
        self.assertIn("data-doc-search", self.page)

    def test_every_link_into_the_docs_lands_on_a_topic(self):
        ids = set(re.findall(r'id="([a-z-]+)"', self.page))
        sources = [*ROOT.joinpath("webmanager", "templates").rglob("*.html"), *ROOT.joinpath("webmanager").glob("*.py")]
        anchors = set()
        for path in sources:
            text = path.read_text(encoding="utf-8")
            anchors.update(re.findall(r"deployments\.docs'\) \}\}#([a-z-]+)", text))
            anchors.update(re.findall(r'deployments\.docs"\) \+ "#([a-z-]+)', text))
            if path.name == "docs.html":
                anchors.update(re.findall(r'href="#([a-z-]+)"', text))
        self.assertTrue({"deploy", "apps", "private-repositories"} <= anchors)
        for anchor in anchors:
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, ids)


if __name__ == "__main__":
    unittest.main()
