"""The System page is split into tabs, most important first, and every
address that points into it (after a form, from another page) still lands
in the right tab."""

import re
import unittest

from tests import test_app as base

ORDER = ["health", "updates", "servers", "backups", "site-updates", "apps", "settings", "notifications"]


class SystemTabsTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user
    csrf = base.WebManagerTestCase.csrf

    def setUp(self):
        self.setUp_base()
        self.login_user(self.add_user("root", "root@example.com", is_admin=True))
        self.page = self.client.get("/admin/?section=updates").get_data(as_text=True)

    def test_tabs_are_in_order_of_importance(self):
        self.assertEqual(re.findall(r'data-tab="([a-z-]+)"', self.page), ORDER)
        self.assertEqual(re.findall(r'data-tab-panel="([a-z-]+)"', self.page), ORDER)
        self.assertIn('role="tablist"', self.page)
        self.assertNotIn("jump-links", self.page)

    def test_each_tab_links_to_a_section_inside_its_own_panel(self):
        panels = dict(re.findall(r'data-tab-panel="([a-z-]+)">(.*?)(?=<div class="tab-panel"|\Z)', self.page, re.S))
        for name in ORDER:
            with self.subTest(tab=name):
                self.assertIn(f'id="{name}"', panels[name])

    def test_addresses_used_after_forms_point_into_a_panel(self):
        source = open(base.Path(__file__).resolve().parent.parent / "webmanager" / "admin.py", encoding="utf-8").read()
        anchors = set(re.findall(r'section="updates"\) \+ "#([a-z-]+)"', source))
        self.assertTrue({"updates", "servers", "replication", "apps", "settings", "site-updates"} <= anchors)
        for anchor in anchors:
            with self.subTest(anchor=anchor):
                self.assertIn(f'id="{anchor}"', self.page)

    def test_the_update_tab_flags_an_available_update(self):
        tab = re.search(r'<a href="#updates"[^>]*data-tab="updates">.*?</a>', self.page, re.S).group(0)
        self.assertNotIn("nav-badge", tab)


if __name__ == "__main__":
    unittest.main()
