import importlib.util
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cachy_update_history", PROJECT_ROOT / "cachy-update-history.py"
)
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class ConfigTests(unittest.TestCase):
    def test_config_round_trip_is_private_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config" / "settings.json"
            with mock.patch.object(APP, "_CONFIG_PATH", str(path)):
                APP._save_config({"lang": "de", "tooltips": False})
                self.assertEqual(
                    APP._load_config(), {"lang": "de", "tooltips": False}
                )

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_config_rejects_non_object_invalid_and_oversized_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "settings.json"
            with mock.patch.object(APP, "_CONFIG_PATH", str(path)):
                path.write_text("[]", encoding="utf-8")
                self.assertEqual(APP._load_config(), {})
                path.write_text("{", encoding="utf-8")
                self.assertEqual(APP._load_config(), {})
                path.write_bytes(b'{"value":"' + b"x" * APP._CONFIG_MAX_BYTES + b'"}')
                self.assertEqual(APP._load_config(), {})


class InputSafetyTests(unittest.TestCase):
    def test_csv_formula_and_control_prefixes_are_neutralised(self):
        for value in ("=1+1", "+cmd", "-2", "@sum", "\tvalue", "\nvalue", "\x00value"):
            with self.subTest(value=value):
                self.assertEqual(APP._csv_safe(value), "'" + value)
        self.assertEqual(APP._csv_safe("ordinary"), "ordinary")
        self.assertEqual(APP._csv_safe(None), "")

    def test_flatpak_ids_reject_path_and_glob_syntax(self):
        self.assertTrue(APP._valid_flatpak_app_id("org.mozilla.firefox"))
        self.assertTrue(APP._valid_flatpak_app_id("io.github.example-App_2"))
        for value in ("../evil.app", "org.example.*", "org/example/App", "header", None):
            with self.subTest(value=value):
                self.assertFalse(APP._valid_flatpak_app_id(value))
                self.assertIsNone(APP._flatpak_deploy_mtime(value))

    def test_flatpak_history_drops_unsafe_ids(self):
        text = (
            "2026-07-01 12:00:00\tupdate\torg.example.App\n"
            "2026-07-01 12:00:00\tupdate\t../../unsafe.app\n"
        )
        rows = APP.parse_flatpak_history_text(text)
        self.assertEqual([row["pkg"] for row in rows], ["org.example.App"])

    def test_package_query_uses_option_separator(self):
        with (
            mock.patch.object(APP, "_command_path", return_value="/usr/bin/pacman"),
            mock.patch.object(APP, "_query_kv", return_value=None) as query,
        ):
            APP._pkg_fields({"source_key": "pacman", "pkg": "--config"})
        query.assert_called_once_with(
            ["/usr/bin/pacman", "-Qi", "--", "--config"]
        )


class DataAndPerformanceTests(unittest.TestCase):
    def test_collect_update_data_parses_pacman_and_marks_foreign_packages(self):
        log = (
            "[2026-07-01T12:00:00+0200] [ALPM] upgraded aurpkg (1.0 -> 2.0)\n"
            "[2026-07-01T12:01:00+0200] [ALPM] installed corepkg (3.0)\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "pacman.log"
            path.write_text(log, encoding="utf-8")
            with (
                mock.patch.object(APP, "get_foreign_packages", return_value={"aurpkg"}),
                mock.patch.object(APP, "_command_path", return_value=None),
            ):
                result = APP.collect_update_data(str(path))

        self.assertIsNone(result["log_error"])
        self.assertFalse(result["flatpak_available"])
        self.assertEqual(
            [(row["pkg"], row["source_key"]) for row in result["rows"]],
            [("aurpkg", "aur"), ("corepkg", "pacman")],
        )

    def test_tooltip_cache_is_lru_bounded(self):
        cache = APP._TOOLTIP_CACHE
        cache.clear()
        with mock.patch.object(APP, "_TOOLTIP_CACHE_LIMIT", 2):
            APP._tooltip_cache_put(("pacman", "one", "en"), "one")
            APP._tooltip_cache_put(("pacman", "two", "en"), "two")
            APP._tooltip_cache_get(("pacman", "one", "en"))
            APP._tooltip_cache_put(("pacman", "three", "en"), "three")

        self.assertIn(("pacman", "one", "en"), cache)
        self.assertNotIn(("pacman", "two", "en"), cache)
        self.assertIn(("pacman", "three", "en"), cache)
        cache.clear()

    def test_filter_reuses_precomputed_search_text(self):
        proxy = APP.UpdateFilterProxy()
        proxy.set_filters("STRASSE", None, None)
        rec = {
            "pkg": "unused",
            "source_label": "unused",
            "action_label": "unused",
            "source_key": "pacman",
            "action_key": "installed",
            "search_text": "paket strasse installiert",
        }
        self.assertTrue(proxy.accepts(rec))


if __name__ == "__main__":
    unittest.main()
