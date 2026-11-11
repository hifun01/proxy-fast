"""Configuration and cloud-request wiring tests; no real cloud resources."""
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from proxy_fast.cli import main, parser
from proxy_fast.cloud import Credentials, ProxyError
from proxy_fast.config import apply_config, load_config
from proxy_fast.manager import Manager, Settings


ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.file = self.root / "proxy-fast.ini"

    def write(self, content):
        self.file.write_text(content, encoding="utf-8")
        return self.file

    def test_cli_env_file_precedence_and_both_config_positions(self):
        self.write("[cloud]\nregion=ap-northeast-1\n[instance]\nprice_limit=0.20\n[network]\nbandwidth_mbps=30\n[disk]\nsize_gib=80\ncategory=cloud_ssd\n")
        for argv in (["--config", str(self.file), "on", "--bandwidth", "5"],
                     ["on", "--config", str(self.file), "--bandwidth", "5"]):
            with patch.dict(os.environ, {"PROXY_FAST_REGION": "eu-central-1", "PROXY_FAST_PRICE_LIMIT": "0.30"}, clear=True):
                arguments = parser().parse_args(argv)
                apply_config(arguments)
                self.assertEqual((arguments.region, arguments.price_limit, arguments.bandwidth), ("eu-central-1", "0.30", 5))
                self.assertEqual((arguments.disk_size, arguments.disk_category), (80, "cloud_ssd"))
        with patch.dict(os.environ, {}, clear=True):
            arguments = parser().parse_args(["on", "--config", str(self.file), "--price-limit", "0.40"])
            apply_config(arguments)
            self.assertEqual((arguments.region, arguments.price_limit), ("ap-northeast-1", "0.40"))

    def test_relative_state_path_uses_config_directory_not_cwd(self):
        self.write("[controller]\nstate_dir=./my-state\n")
        values, path = load_config(self.file, env={}, cwd=self.root.parent)
        self.assertEqual(values["state_dir"], self.root / "my-state")
        self.assertEqual(path, self.file)

    def test_discovery_cwd_before_repository_and_explicit_missing_error(self):
        repository = self.root / "repo"; repository.mkdir()
        (repository / "pyproject.toml").touch()
        (repository / "proxy-fast.ini").write_text("[cloud]\nregion=eu-central-1\n")
        values, _ = load_config(env={}, cwd=self.root, repository=repository)
        self.assertEqual(values["region"], "eu-central-1")
        self.write("[cloud]\nregion=ap-southeast-1\n")
        values, _ = load_config(env={}, cwd=self.root, repository=repository)
        self.assertEqual(values["region"], "ap-southeast-1")
        with self.assertRaises(ProxyError): load_config(self.root / "missing.ini", env={})
        values, _ = load_config(env={"PROXY_FAST_CONFIG": str(repository / "proxy-fast.ini")}, cwd=self.root)
        self.assertEqual(values["region"], "eu-central-1")

    def test_missing_optional_config_keeps_existing_defaults(self):
        with patch("proxy_fast.config.load_config", return_value=({}, None)), patch.dict(os.environ, {}, clear=True):
            arguments = parser().parse_args(["on"])
            apply_config(arguments)
        self.assertEqual((arguments.price_limit, arguments.bandwidth, arguments.protection, arguments.disk_size, arguments.timeout),
                         ("0.10", 100, 1, 20, 600))

    def test_typos_duplicate_keys_and_bad_integers_are_rejected(self):
        for text in ("[netwrok]\nbandwidth_mbps=20", "[network]\nbandwith_mbps=20",
                     "[disk]\nsize_gib=20.5", "[disk]\nsize_gib=20\nsize_gib=40",
                     "[DEFAULT]\nregion=abc", "not an ini file"):
            self.write(text)
            with self.subTest(text=text), self.assertRaises(ProxyError): load_config(self.file, env={})

    def test_chinese_comments_bom_empty_optionals_and_percent_in_state_path(self):
        self.file.write_text("\ufeff# 中文注释\n[cloud]\nprofile=\n[instance]\nzone=\n[controller]\nstate_dir=state%name\n", encoding="utf-8")
        values, _ = load_config(self.file, env={})
        self.assertNotIn("profile", values)
        self.assertNotIn("zone", values)
        self.assertEqual(values["state_dir"], self.root / "state%name")

    def test_invalid_cost_related_settings_fail_before_credentials_or_cloud(self):
        for section, option, value in (("instance", "price_limit", "NaN"), ("instance", "price_limit", "-1"),
                                       ("network", "bandwidth_mbps", "0"), ("instance", "protection_hours", "2"),
                                       ("disk", "size_gib", "10"), ("disk", "category", "unknown")):
            self.write(f"[{section}]\n{option}={value}\n")
            with patch.dict(os.environ, {}, clear=True), patch("proxy_fast.cli.load_credentials") as credentials, redirect_stderr(io.StringIO()):
                self.assertEqual(main(["on", "--config", str(self.file)]), 1)
                credentials.assert_not_called()

    def test_config_is_wired_to_manager_and_off_uses_saved_region(self):
        self.write("[cloud]\nregion=ap-northeast-1\nprofile=my-profile\n[instance]\nprice_limit=0.25\n[disk]\nsize_gib=80\ncategory=cloud_ssd\n[network]\nbandwidth_mbps=20\n[controller]\nstate_dir=state\ntimeout_seconds=90\n")
        cloud = SimpleNamespace(account_id=lambda: "123456789")
        captured = []
        def manager(cloud, store, settings, bootstrap, **kwargs):
            captured.append(settings)
            return SimpleNamespace(on=lambda: {"status": "ready", "subscriptions": {"mihomo": "test", "sing_box": "test"}},
                                   off=lambda: {"status": "off"})
        with patch.dict(os.environ, {}, clear=True), patch("proxy_fast.cli.load_credentials", return_value=Credentials("test", "test")) as credentials, patch("proxy_fast.cli.Manager", side_effect=manager), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["on", "--config", str(self.file)], cloud_factory=lambda *_: cloud), 0)
            credentials.assert_called_with("my-profile")
            first = captured[-1]
            self.assertEqual((first.region, first.price_limit, first.bandwidth, first.disk_size, first.disk_category, first.timeout),
                             ("ap-northeast-1", "0.25", 20, 80, "cloud_ssd", 90))
            self.write(self.file.read_text().replace("ap-northeast-1", "eu-central-1"))
            self.assertEqual(main(["off", "--config", str(self.file)], cloud_factory=lambda *_: cloud), 0)
            self.assertEqual(captured[-1].region, "ap-northeast-1")

    def test_disk_size_accounts_for_image_minimum_and_requested_type_is_strict(self):
        def call(service, action, **kwargs):
            if action == "DescribeImages":
                return {"Images": {"Image": [{"ImageId": "ubuntu", "OSNameEn": "Ubuntu 24.04", "Size": 40}]}}
            return {"AvailableZones": {"AvailableZone": [{"ZoneId": "zone", "Status": "Available", "AvailableResources": {
                "AvailableResource": [{"Type": "SystemDisk", "SupportedResources": {"SupportedResource": [
                    {"Value": "cloud_efficiency", "Status": "Available", "StatusCategory": "WithStock"}]}}]}}]}}
        manager = Manager(SimpleNamespace(call=call), None, Settings(disk_size=80), ROOT / "proxy-fast.sh")
        self.assertEqual(manager.image(), ("ubuntu", 80))
        manager.settings.disk_size = 20
        self.assertEqual(manager.image(), ("ubuntu", 40))
        self.assertEqual(manager.disk_category("zone", "type"), "cloud_efficiency")
        manager.settings.disk_category = "cloud_essd"
        self.assertIsNone(manager.disk_category("zone", "type"))


if __name__ == "__main__": unittest.main()
