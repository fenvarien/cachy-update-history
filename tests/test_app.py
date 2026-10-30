import importlib.util
import locale
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
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

    def test_pacman_install_date_is_language_neutral(self):
        info = {"Install Date": "Tue Oct 20 18:55:29 2026"}
        with (
            mock.patch.object(APP, "_command_path", return_value="/usr/bin/pacman"),
            mock.patch.object(APP, "_query_kv", return_value=(None, info)),
        ):
            for lang in ("en", "de"):
                with self.subTest(lang=lang):
                    fields = dict(APP._pkg_fields(
                        {"source_key": "pacman", "pkg": "firefox"}, lang))
                    self.assertEqual(fields[APP.tr("tt_install_date", lang)],
                                     "2026-10-20  18:55:29")
        self.assertEqual(APP._format_pacman_date("Mon Mar  3 09:05:01 2025"),
                         "2025-03-03  09:05:01")
        for raw in (None, "", "gestern", "Fri Feb 30 10:00:00 2026"):
            with self.subTest(raw=raw):
                self.assertEqual(APP._format_pacman_date(raw), raw)


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

    def test_filter_matches_multi_tokens_versions_and_dates(self):
        proxy = APP.UpdateFilterProxy()
        rec = {
            "pkg": "linux-cachyos",
            "source_label": "Pacman",
            "action_label": "Upgraded",
            "source_key": "pacman",
            "action_key": "upgraded",
            "old": "6.11.0-1",
            "new": "6.12.0-1",
            "display": "2026-09-17  22:02",
        }
        APP.MainWindow._translate_row(rec)

        # Multi-word out of order
        proxy.set_filters("6.12 linux", None, None)
        self.assertTrue(proxy.accepts(rec))

        # Search by date
        proxy.set_filters("2026-09-17", None, None)
        self.assertTrue(proxy.accepts(rec))

        # Search by old version
        proxy.set_filters("6.11.0", None, None)
        self.assertTrue(proxy.accepts(rec))

        # Search mismatch
        proxy.set_filters("firefox", None, None)
        self.assertFalse(proxy.accepts(rec))

    def _check_proxy_filter_updates(self, proxy):
        model = APP.UpdateTableModel()
        model.set_rows([
            {"pkg": "linux-cachyos", "source_key": "pacman",
             "source_label": "Pacman", "action_key": "upgraded",
             "action_label": "Upgraded"},
            {"pkg": "example-aur", "source_key": "aur",
             "source_label": "AUR", "action_key": "installed",
             "action_label": "Installed"},
            {"pkg": "org.example.App", "source_key": "flatpak",
             "source_label": "Flatpak", "action_key": "upgraded",
             "action_label": "Upgraded"},
        ])
        proxy.setSourceModel(model)
        self.assertEqual(proxy.rowCount(), 3)
        for filters, packages in (
            (("", None, "upgraded"), ["linux-cachyos", "org.example.App"]),
            (("", "aur", None), ["example-aur"]),
            (("EXAMPLE upgraded", "flatpak", "upgraded"), ["org.example.App"]),
            (("missing", None, None), []),
            (("", None, None), ["linux-cachyos", "example-aur", "org.example.App"]),
        ):
            with self.subTest(filters=filters):
                proxy.set_filters(*filters)
                self.assertEqual(
                    [proxy.index(row, 3).data() for row in range(proxy.rowCount())],
                    packages,
                )
                self.assertEqual(proxy.columnCount(), 6)

    def test_filter_updates_attached_model_with_modern_qt_api(self):
        proxy = APP.UpdateFilterProxy()
        if not callable(getattr(proxy, "endFilterChange", None)):
            self.skipTest("Qt 6.10 filter-change API not available")
        with mock.patch.object(
            proxy, "invalidateFilter",
            side_effect=AssertionError("Modern Qt must use the filter-change API"),
        ):
            self._check_proxy_filter_updates(proxy)

    def test_filter_updates_attached_model_without_modern_qt_api(self):
        proxy = APP.UpdateFilterProxy()
        with (
            mock.patch.object(proxy, "endFilterChange", None, create=True),
            mock.patch.object(proxy, "invalidateFilter",
                              wraps=proxy.invalidateFilter) as invalidate,
        ):
            self._check_proxy_filter_updates(proxy)
        self.assertEqual(invalidate.call_count, 5)

    def test_parse_timestamp_iso_and_legacy(self):
        # Modern ISO-8601 pacman format
        dt_iso = APP.parse_timestamp("2026-09-17T22:02:59+0200")
        self.assertIsNotNone(dt_iso)
        self.assertEqual(dt_iso.year, 2026)
        self.assertEqual(dt_iso.month, 9)
        self.assertEqual(dt_iso.day, 17)

        # Legacy pacman format
        dt_legacy = APP.parse_timestamp("2023-01-02 09:15")
        self.assertIsNotNone(dt_legacy)
        self.assertEqual(dt_legacy.year, 2023)
        self.assertEqual(dt_legacy.minute, 15)

        # Flatpak format
        dt_fp = APP.parse_timestamp("Jun 20 10:57:01")
        self.assertIsNotNone(dt_fp)
        self.assertEqual(dt_fp.month, 6)
        self.assertEqual(dt_fp.day, 20)

        # Invalid
        self.assertIsNone(APP.parse_timestamp("not-a-timestamp"))

    def test_collect_update_data_fast_skips_non_alpm(self):
        log = (
            "[2026-09-17T22:02:48+0200] [PACMAN] Running 'pacman -Syu'\n"
            "[2026-09-17T22:02:58+0200] [ALPM-SCRIPTLET] ==> hook\n"
            "[2026-09-17T22:02:59+0200] [ALPM] upgraded mypkg (1.0 -> 2.0)\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "pacman.log"
            path.write_text(log, encoding="utf-8")
            with (
                mock.patch.object(APP, "get_foreign_packages", return_value=set()),
                mock.patch.object(APP, "_command_path", return_value=None),
            ):
                result = APP.collect_update_data(str(path))

        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["pkg"], "mypkg")
        self.assertEqual(result["rows"][0]["old"], "1.0")
        self.assertEqual(result["rows"][0]["new"], "2.0")

    def test_parse_timestamp_yearless_ignores_process_locale(self):
        # QApplication switches LC_TIME to the user's locale; "Mar"/"Oct"
        # must still parse on e.g. German systems.
        original = locale.setlocale(locale.LC_TIME)
        for name in ("de_DE.UTF-8", "de_DE.utf8"):
            try:
                locale.setlocale(locale.LC_TIME, name)
                break
            except locale.Error:
                continue
        else:
            self.skipTest("German locale not available")
        try:
            for raw, month in (("Mar  3 10:57:01", 3), ("Oct 20 09:00", 10),
                               ("Dec 31 23:59:59", 12)):
                with self.subTest(raw=raw):
                    dt = APP.parse_timestamp(raw)
                    self.assertIsNotNone(dt)
                    self.assertEqual(dt.month, month)
        finally:
            locale.setlocale(locale.LC_TIME, original)

    def test_parse_timestamp_yearless_year_rollover_and_leap_day(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2025, 3, 10, 12, 0)

        with mock.patch.object(APP, "datetime", FixedDateTime):
            # Feb 29 does not exist in 2025 -> previous (leap) year
            self.assertEqual(APP.parse_timestamp("Feb 29 08:00:00"),
                             datetime(2024, 2, 29, 8, 0))
            # A date after "now" belongs to the previous year
            self.assertEqual(APP.parse_timestamp("Dec 24 18:30"),
                             datetime(2024, 12, 24, 18, 30))
            self.assertEqual(APP.parse_timestamp("Mar  1 07:05:09"),
                             datetime(2025, 3, 1, 7, 5, 9))
            self.assertIsNone(APP.parse_timestamp("Foo 01 10:00"))
            self.assertIsNone(APP.parse_timestamp("Feb 30 10:00"))

    def test_cli_version_and_help(self):
        script = str(PROJECT_ROOT / "cachy-update-history.py")
        res_v = subprocess.run(
            [sys.executable, script, "--version"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(res_v.returncode, 0)
        self.assertIn(APP.__version__, res_v.stdout)

        res_h = subprocess.run(
            [sys.executable, script, "--help"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(res_h.returncode, 0)
        self.assertIn("Usage:", res_h.stdout)

if __name__ == "__main__":
    unittest.main()
