"""Eventual ownership-tag visibility must never bypass ownership checks."""
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest

from proxy_fast.cloud import ProxyError
from proxy_fast.manager import Manager, Settings, OWNER_TAG, ROLE_TAG, OwnershipTagsPending


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.ticks, self.calls, self.messages = 0, [], []
        self.complete = {"VpcId": "vpc-test", "Status": "Available", "Tags": {"Tag": [
            {"Key": OWNER_TAG, "Value": "owner-test"}, {"Key": ROLE_TAG, "Value": "vpc"}]}}
        self.sequence = []
        self.manager = Manager(SimpleNamespace(call=self.call), SimpleNamespace(data={"owner_id": "owner-test"}),
                               Settings(), Path("proxy-fast.sh"), sleep=self.sleep, clock=lambda: self.ticks,
                               log=self.messages.append)

    def sleep(self, seconds): self.ticks += seconds

    def call(self, service, action, **params):
        self.calls.append((service, action, params))
        self.assertEqual(action, "DescribeVpcs")  # No writes allowed in any visibility retry.
        value = self.sequence.pop(0) if self.sequence else self.complete
        return {"Vpcs": {"Vpc": [copy.deepcopy(value)]}, "TotalCount": 1}

    def missing(self):
        value = copy.deepcopy(self.complete); value.pop("Tags")
        return value

    def test_new_vpc_missing_then_partial_then_complete_tags(self):
        partial = copy.deepcopy(self.complete); partial["Tags"]["Tag"].pop()
        self.sequence = [self.missing(), partial, self.complete]
        self.assertEqual(self.manager.resource("vpc", "vpc-test"), self.complete)
        self.assertEqual(self.ticks, 2)
        self.assertEqual(len(self.messages), 1)

    def test_wrong_owner_is_rejected_immediately_even_with_missing_role(self):
        wrong = copy.deepcopy(self.complete)
        wrong["Tags"]["Tag"] = [{"Key": OWNER_TAG, "Value": "someone-else"}]
        self.sequence = [wrong]
        with self.assertRaisesRegex(ProxyError, "do not match"): self.manager.resource("vpc", "vpc-test")
        self.assertEqual(self.ticks, 0)
        self.assertEqual(len(self.calls), 1)

    def test_wrong_role_is_rejected_immediately(self):
        wrong = copy.deepcopy(self.complete)
        wrong["Tags"]["Tag"][1]["Value"] = "vswitch"
        self.sequence = [wrong]
        with self.assertRaisesRegex(ProxyError, "do not match"): self.manager.resource("vpc", "vpc-test")
        self.assertEqual(self.ticks, 0)

    def test_permanently_missing_tags_timeout_without_managing_resource(self):
        self.sequence = [self.missing() for _ in range(31)]
        with self.assertRaisesRegex(OwnershipTagsPending, "still missing"): self.manager.resource("vpc", "vpc-test")
        self.assertEqual(self.ticks, 30)
        self.assertEqual(len(self.calls), 31)

    def test_owned_list_requeries_missing_tags_by_exact_resource_id(self):
        self.sequence = [self.missing(), self.missing(), self.complete]
        self.assertEqual(self.manager.resources("vpc", owned=True), [self.complete])
        self.assertEqual(self.calls[0][2]["Tag.1.Value"], "owner-test")
        self.assertEqual(self.calls[1][2]["VpcId"], "vpc-test")
        self.assertEqual(self.ticks, 1)

    def test_changed_owner_during_retry_stops_immediately(self):
        wrong = copy.deepcopy(self.complete)
        wrong["Tags"]["Tag"][0]["Value"] = "someone-else"
        self.sequence = [self.missing(), wrong]
        with self.assertRaisesRegex(ProxyError, "do not match"): self.manager.resource("vpc", "vpc-test")
        self.assertEqual(self.ticks, 1)


if __name__ == "__main__": unittest.main()
