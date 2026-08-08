"""Proxy pools rotate API and resource egress independently."""

import unittest

from app.control.proxy import ProxyDirectory
from app.control.proxy.models import EgressMode, EgressNode, ProxyScope


class ProxyRotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_robin_uses_separate_api_and_resource_pools(self) -> None:
        directory = ProxyDirectory()
        directory._egress_mode = EgressMode.PROXY_POOL
        directory._nodes = [
            EgressNode(node_id="api-1", proxy_url="http://api-1"),
            EgressNode(node_id="api-2", proxy_url="http://api-2"),
        ]
        directory._resource_nodes = [
            EgressNode(node_id="resource-1", proxy_url="http://resource-1"),
            EgressNode(node_id="resource-2", proxy_url="http://resource-2"),
        ]

        api = [(await directory.acquire()).proxy_url for _ in range(3)]
        resources = [
            (await directory.acquire(scope=ProxyScope.ASSET)).proxy_url
            for _ in range(3)
        ]

        self.assertEqual(api, ["http://api-1", "http://api-2", "http://api-1"])
        self.assertEqual(
            resources,
            ["http://resource-1", "http://resource-2", "http://resource-1"],
        )


if __name__ == "__main__":
    unittest.main()
