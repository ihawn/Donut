import io
import os
import unittest
from unittest.mock import patch
from donut.catalog import endpoint_models


class CatalogTests(unittest.TestCase):
    def test_catalog_uses_connection_key_and_validates_model_records(self):
        profile = {
            "engine": "openai",
            "model": "existing",
            "base_url": "https://example.com/v1",
            "key_env": "DONUT_TEST_KEY",
        }
        with (
            patch.dict(os.environ, {"DONUT_TEST_KEY": "fixture"}),
            patch(
                "donut.catalog.urlopen", return_value=io.BytesIO(b'{"data":[{"id":"one"},{"id":"two"},{"id":4},null]}')
            ) as call,
        ):
            self.assertEqual([row["id"] for row in endpoint_models(profile)], ["one", "two"])
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.com/v1/models")
        self.assertEqual(request.headers["Authorization"], "Bearer fixture")
