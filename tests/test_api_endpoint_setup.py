import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import api_endpoint_setup
import br_client
import endpoint_input


class ApiEndpointSetupTests(unittest.TestCase):
    VALUES = {
        "text": "https://text.proxy.example/api",
        "image": "https://image.proxy.example/api",
        "video": "https://video.proxy.example/api",
    }

    def test_save_and_load_three_distinct_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                os.environ,
                {"BASICROUTER_ENDPOINTS_FILE": os.path.join(tmp, "endpoints.json")},
                clear=True):
            path = api_endpoint_setup.save_endpoints(self.VALUES)
            self.assertEqual(api_endpoint_setup.load_endpoints(allow_default=False), self.VALUES)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(payload["endpoints"], self.VALUES)

    def test_all_three_fields_are_required_and_distinct(self):
        with self.assertRaises(api_endpoint_setup.EndpointSetupError):
            api_endpoint_setup.validate_endpoints({
                "text": "https://same.example/api",
                "image": "https://same.example/api",
                "video": "https://video.example/api",
            })
        with self.assertRaises(api_endpoint_setup.EndpointSetupError):
            api_endpoint_setup.validate_endpoints({
                "text": "https://text.example/api",
                "image": "",
                "video": "https://video.example/api",
            })

    def test_request_paths_route_to_the_correct_endpoint_kind(self):
        self.assertEqual(br_client._api_kind_for_request("/v1/chat/completions"), "text")
        self.assertEqual(br_client._api_kind_for_request("/v1/image-generations"), "image")
        self.assertEqual(br_client._api_kind_for_request("/v1/video-generations"), "video")
        self.assertEqual(br_client._api_kind_for_request(
            "/employee/models", {"category": "image"}), "image")
        self.assertEqual(br_client._api_kind_for_request(
            "/employee/models", {"category": "video"}), "video")

    def test_request_uses_selected_gateway_at_call_time(self):
        response = mock.MagicMock()
        response.read.return_value = b'{}'
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with mock.patch.object(
                br_client.api_endpoint_setup, "base_url",
                side_effect=lambda kind: self.VALUES[kind]) as resolver, \
                mock.patch.object(
                    br_client.urllib.request, "urlopen",
                    return_value=response) as opener:
            br_client._request("GET", "/v1/image-models", max_retries=0)
            self.assertEqual(opener.call_args.args[0].full_url,
                             self.VALUES["image"] + "/v1/image-models")
            resolver.assert_called_once_with("image")

    def test_environment_requires_the_complete_three_url_set(self):
        with mock.patch.dict(os.environ, {
                "BASICROUTER_TEXT_BASE_URL": self.VALUES["text"]}, clear=True):
            with self.assertRaises(api_endpoint_setup.EndpointSetupError):
                api_endpoint_setup.explicit_endpoints()

    def test_legacy_single_url_does_not_bypass_new_initialization(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                "BASICROUTER_BASE_URL": "https://legacy.proxy.example/api",
                "BASICROUTER_ENDPOINTS_FILE": os.path.join(tmp, "missing.json"),
        }, clear=True):
            self.assertIsNone(api_endpoint_setup.explicit_endpoints())
            self.assertEqual(api_endpoint_setup.load_endpoints()["video"],
                             "https://legacy.proxy.example/api")

    def test_macos_native_fallback_collects_all_three_fields(self):
        completed = [
            mock.Mock(returncode=0, stdout=self.VALUES[kind] + "\n")
            for kind in api_endpoint_setup.ENDPOINT_KINDS
        ]
        with mock.patch.object(endpoint_input.shutil, "which", return_value="/usr/bin/osascript"), \
                mock.patch.object(endpoint_input.subprocess, "run", side_effect=completed) as runner:
            self.assertEqual(endpoint_input._macos_form(), self.VALUES)
        self.assertEqual(runner.call_count, 3)


if __name__ == "__main__":
    unittest.main()
