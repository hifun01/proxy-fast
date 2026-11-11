"""One managed spot instance, with recoverable creation and scoped deletion."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
import ipaddress
import json
import re
from pathlib import Path
import secrets
import socket
import time
import urllib.error
import urllib.request
import uuid

from .cloud import ApiError, ProxyError
from .state import Store


OWNER_TAG = "proxy-fast-owner"
ROLE_TAG = "proxy-fast-role"


class OwnershipTagsPending(ProxyError):
    """The describe response has not returned both ownership tags yet."""


def tags(resource: dict) -> dict:
    values = resource.get("Tags", {}).get("Tag", [])
    return {t.get("Key", t.get("TagKey")): t.get("Value", t.get("TagValue")) for t in values}


def absent(error: ApiError) -> bool:
    code = error.code.lower()
    return "notfound" in code or "notexist" in code or "not_found" in code


def capacity_rejected(error: ApiError) -> bool:
    return any(v in error.code.lower() for v in (
        "stock", "soldout", "notenough", "insufficientinstancecapacity", "insufficientresourcecapacity",
        "spotpric", "notsupported", "notonsale"))


@dataclass
class Settings:
    region: str = "eu-west-2"
    price_limit: str = "0.10"
    bandwidth: int = 100
    protection_hours: int = 1
    timeout: int = 600
    zone: str | None = None
    instance_type: str | None = None
    image_id: str | None = None
    disk_size: int = 20
    disk_category: str = "auto"

    def validate(self):
        try:
            price = Decimal(self.price_limit)
            if not price.is_finite() or price <= 0:
                raise InvalidOperation
        except InvalidOperation as exc:
            raise ProxyError("--price-limit must be a positive hourly compute bid in the account's billing currency.") from exc
        if not 1 <= self.bandwidth <= 100:
            raise ProxyError("--bandwidth must be 1..100 Mbit/s.")
        if self.protection_hours not in (0, 1) or not 30 <= self.timeout <= 3600:
            raise ProxyError("Protection must be 0/1 hour; timeout must be 30..3600 seconds.")
        if not 20 <= self.disk_size <= 2048:
            raise ProxyError("Disk size must be 20..2048 GiB.")
        if self.disk_category not in ("auto", "cloud_essd", "cloud_ssd", "cloud_efficiency"):
            raise ProxyError("Disk category must be auto/cloud_essd/cloud_ssd/cloud_efficiency.")


def make_user_data(bootstrap: bytes, token: str, deployment_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) or not re.fullmatch(r"[a-f0-9]{32}", deployment_id):
        raise ProxyError("Invalid local deployment identifier; preserve state for recovery.")
    # Embed the local, tested installer rather than executing a moving GitHub branch.
    packed = base64.b64encode(gzip.compress(bootstrap, mtime=0)).decode()
    digest = hashlib.sha256(bootstrap).hexdigest()
    script = f"""#!/bin/bash
set -Eeuo pipefail
umask 077
mkdir -p /var/log/proxy-fast /run/proxy-fast-bootstrap
exec > /var/log/proxy-fast/bootstrap.log 2>&1
export PROXY_FAST_SUB_TOKEN='{token}'
export PROXY_FAST_DEPLOYMENT_ID='{deployment_id}'
base64 -d <<'PROXY_FAST_PAYLOAD' | gzip -d > /run/proxy-fast-bootstrap/proxy-fast.sh
{packed}
PROXY_FAST_PAYLOAD
echo '{digest}  /run/proxy-fast-bootstrap/proxy-fast.sh' | sha256sum --check --status
bash /run/proxy-fast-bootstrap/proxy-fast.sh
"""
    if len(script.encode()) > 32768:
        raise ProxyError("Compressed bootstrap exceeds the ECS 32 KiB UserData limit.")
    return base64.b64encode(script.encode()).decode()


def probe_ready(ip: str, token: str, deployment_id: str) -> dict | None:
    address = ipaddress.ip_address(ip)
    if address.version != 4 or not address.is_global:
        raise ProxyError("ECS did not return a public IPv4 address.")
    url = f"http://{ip}:8080/{token}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url + "/ready.json", timeout=3) as response:
            data = json.loads(response.read(65536))
        if (data.get("deployment_id") != deployment_id or data.get("public_ipv4") != ip
                or data.get("proxy_egress_verified") is not True):
            return None
        if data.get("mihomo") != url + "/clmi.yaml" or data.get("sing_box") != url + "/sbox.json":
            raise ProxyError("Remote subscription result does not match this deployment.")
        for name in ("clmi.yaml", "sbox.json"):
            with opener.open(url + "/" + name, timeout=3) as response:
                json.loads(response.read(262144))
        with socket.create_connection((ip, 443), timeout=3):
            pass
        return data
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


class Manager:
    KINDS = {
        "instance": ("ecs", "DescribeInstances", "Instances", "Instance", "InstanceId"),
        "security-group": ("ecs", "DescribeSecurityGroups", "SecurityGroups", "SecurityGroup", "SecurityGroupId"),
        "vpc": ("vpc", "DescribeVpcs", "Vpcs", "Vpc", "VpcId"),
        "vswitch": ("vpc", "DescribeVSwitches", "VSwitches", "VSwitch", "VSwitchId"),
    }

    def __init__(self, cloud, store: Store, settings: Settings, bootstrap: Path,
                 log=lambda message: None, probe=probe_ready, sleep=time.sleep, clock=time.monotonic):
        self.cloud, self.store, self.settings = cloud, store, settings
        self.bootstrap, self.log, self.probe = Path(bootstrap), log, probe
        self.sleep, self.clock = sleep, clock
        self.settings.validate()

    @property
    def state(self):
        return self.store.data

    def checkpoint(self, **values):
        self.state.update(values); self.store.save()

    def owner_params(self, role=None):
        params = {"Tag.1.Key": OWNER_TAG, "Tag.1.Value": self.state["owner_id"]}
        if role:
            params.update({"Tag.2.Key": ROLE_TAG, "Tag.2.Value": role})
        return params

    def assert_owned(self, resource: dict, role: str):
        values = tags(resource)
        expected = {OWNER_TAG: self.state["owner_id"], ROLE_TAG: role}
        if any(key in values and values[key] != value for key, value in expected.items()):
            raise ProxyError(f"Refusing to manage {role}: ownership tags do not match this local state.")
        if any(key not in values for key in expected):
            raise OwnershipTagsPending(f"Refusing to manage {role}: ownership tags are still missing from the cloud response. "
                                       "State retained; retry after tags become visible or check tags in the ECS/VPC console.")

    def resources(self, kind: str, resource_id=None, owned=False):
        service, action, container, item, id_key = self.KINDS[kind]
        params = self.owner_params(kind) if owned else {}
        if resource_id:
            params["InstanceIds" if kind == "instance" else id_key] = json.dumps([resource_id]) if kind == "instance" else resource_id
        result, page = [], 1
        while True:
            try:
                data = self.cloud.call(service, action, PageNumber=page, PageSize=50, **params)
            except ApiError as exc:
                if resource_id and absent(exc):
                    return []
                raise
            rows = data.get(container, {}).get(item, [])
            result.extend(rows)
            if len(result) >= int(data.get("TotalCount", len(result))) or len(rows) < 50:
                break
            page += 1
        if owned:
            checked = []
            for value in result:
                try:
                    self.assert_owned(value, kind)
                except OwnershipTagsPending:
                    # Tag-filtered list results may appear before Tags is populated.
                    resource_id = value.get(id_key)
                    if not resource_id:
                        raise ProxyError(f"Unexpected {kind} lookup response without a resource ID.")
                    value = self.resource(kind, resource_id)
                    if value is None:
                        continue
                checked.append(value)
            result = checked
        return result

    def resource(self, kind: str, resource_id: str):
        deadline = self.clock() + 30
        logged = False
        while True:
            rows = self.resources(kind, resource_id)
            if not rows:
                return None
            if len(rows) != 1:
                raise ProxyError(f"Unexpected {kind} lookup response.")
            if rows[0].get(self.KINDS[kind][4]) != resource_id:
                raise ProxyError(f"Unexpected {kind} ID in lookup response.")
            try:
                self.assert_owned(rows[0], kind)
                return rows[0]
            except OwnershipTagsPending:
                if self.clock() >= deadline:
                    raise
                if not logged:
                    self.log(f"Waiting for {kind} {resource_id} ownership tags to become visible.")
                    logged = True
                self.sleep(1)

    def token(self, name: str):
        tokens = self.state.setdefault("tokens", {})
        if name not in tokens:
            tokens[name] = uuid.uuid4().hex
            self.store.save()  # Persist BEFORE sending any resource-creation request.
        return tokens[name]

    def _wait_network(self, kind, resource_id, timeout=90):
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            value = self.resource(kind, resource_id)
            if value and value.get("Status", "Available") == "Available":
                return value
            self.sleep(2)
        raise ProxyError(f"Timed out waiting for {kind} {resource_id} to become Available.")

    def inventory(self) -> list[tuple[str, str]]:
        types, next_token = {}, None
        while True:
            params = dict(MinimumCpuCoreCount=1, MaximumCpuCoreCount=2,
                          MinimumMemorySize=1, MaximumMemorySize=4, CpuArchitecture="X86", MaxResults=100)
            if next_token:
                params["NextToken"] = next_token
            data = self.cloud.call("ecs", "DescribeInstanceTypes", **params)
            for value in data.get("InstanceTypes", {}).get("InstanceType", []):
                if (int(value.get("GPUAmount", 0)) == 0 and int(value.get("LocalStorageAmount", 0)) == 0
                        and value.get("CpuArchitecture", "X86").upper() == "X86"):
                    types[value["InstanceTypeId"]] = value
            next_token = data.get("NextToken")
            if not next_token:
                break
        query = dict(DestinationResource="InstanceType", IoOptimized="optimized",
                     InstanceChargeType="PostPaid", SpotStrategy="SpotWithPriceLimit",
                     SpotDuration=self.settings.protection_hours, ResourceType="instance")
        if self.settings.zone:
            query["ZoneId"] = self.settings.zone
        if self.settings.instance_type:
            query["InstanceType"] = self.settings.instance_type
        data = self.cloud.call("ecs", "DescribeAvailableResource", **query)
        choices = set()
        for zone in data.get("AvailableZones", {}).get("AvailableZone", []):
            if zone.get("Status") != "Available":
                continue
            for group in zone.get("AvailableResources", {}).get("AvailableResource", []):
                if group.get("Type") != "InstanceType":
                    continue
                for value in group.get("SupportedResources", {}).get("SupportedResource", []):
                    name = value.get("Value")
                    if (value.get("Status") == "Available" and value.get("StatusCategory", "WithStock") == "WithStock"
                            and name in types and (not self.settings.instance_type or name == self.settings.instance_type)):
                        choices.add((zone["ZoneId"], name))
        choices = sorted(choices, key=lambda x: (types[x[1]]["CpuCoreCount"], types[x[1]]["MemorySize"], x[1], x[0]))
        if not choices:
            raise ProxyError("No in-stock X86 spot instances with 1–2 vCPU and 1–4 GiB RAM. "
                             "Try another --region/--zone; no regular-priced instance will be substituted.")
        return choices[:24]

    def image(self) -> tuple[str, int]:
        values, page = [], 1
        while True:
            params = dict(ImageOwnerAlias="system", Architecture="x86_64", OSType="linux",
                          Status="Available", IsSupportCloudinit=True, PageNumber=page, PageSize=100)
            if self.settings.image_id:
                params["ImageId"] = self.settings.image_id
            data = self.cloud.call("ecs", "DescribeImages", **params)
            rows = data.get("Images", {}).get("Image", [])
            values.extend(rows)
            if len(values) >= int(data.get("TotalCount", len(values))) or len(rows) < 100:
                break
            page += 1
        def score(row):
            text = " ".join(str(row.get(k, "")) for k in ("Platform", "OSNameEn", "ImageName", "OSName")).lower()
            return (2 if any(v in text for v in ("24.04", "24_04", "24 04")) else
                    1 if any(v in text for v in ("22.04", "22_04", "22 04")) else 0) if "ubuntu" in text else 0
        values = [v for v in values if score(v)]
        if not values:
            raise ProxyError("No official Ubuntu 24.04/22.04 X86 cloud-init image found in this region.")
        selected = max(values, key=lambda row: (score(row), row.get("CreationTime", "")))
        return selected["ImageId"], max(self.settings.disk_size, int(selected.get("Size", 20)))

    def disk_category(self, zone: str, instance_type: str) -> str | None:
        data = self.cloud.call("ecs", "DescribeAvailableResource", DestinationResource="SystemDisk",
            ZoneId=zone, InstanceType=instance_type, IoOptimized="optimized", ResourceType="instance",
            InstanceChargeType="PostPaid", SpotStrategy="SpotWithPriceLimit",
            SpotDuration=self.settings.protection_hours)
        available = set()
        for value in data.get("AvailableZones", {}).get("AvailableZone", []):
            if value.get("ZoneId") != zone or value.get("Status") != "Available":
                continue
            for group in value.get("AvailableResources", {}).get("AvailableResource", []):
                if group.get("Type") != "SystemDisk":
                    continue
                for disk in group.get("SupportedResources", {}).get("SupportedResource", []):
                    if disk.get("Status") == "Available" and disk.get("StatusCategory", "WithStock") == "WithStock":
                        available.add(disk.get("Value"))
        preferences = (("cloud_essd", "cloud_ssd", "cloud_efficiency") if self.settings.disk_category == "auto"
                       else (self.settings.disk_category,))
        return next((v for v in preferences if v in available), None)

    def ensure_network(self):
        name = "proxy-fast-" + self.state["owner_id"][:8]
        for kind, field in (("vpc", "vpc_id"), ("security-group", "security_group_id")):
            resource_id = self.state.get(field)
            value = self.resource(kind, resource_id) if resource_id else None
            if not value:
                recovered = self.resources(kind, owned=True)
                if len(recovered) > 1:
                    raise ProxyError(f"Multiple owned {kind} resources found; run off to clean up first.")
                if recovered:
                    value = recovered[0]; resource_id = value[self.KINDS[kind][4]]
                else:
                    params = dict(ClientToken=self.token(kind), **self.owner_params(kind))
                    if kind == "vpc":
                        response = self.cloud.call("vpc", "CreateVpc", VpcName=name, CidrBlock="10.77.0.0/16", **params)
                        resource_id = response["VpcId"]
                    else:
                        response = self.cloud.call("ecs", "CreateSecurityGroup", VpcId=self.state["vpc_id"],
                            SecurityGroupName=name, SecurityGroupType="normal", Description="proxy-fast dedicated proxy ports", **params)
                        resource_id = response["SecurityGroupId"]
                self.checkpoint(**{field: resource_id})
            self._wait_network(kind, resource_id)
            if kind == "security-group":
                group = self.resource(kind, resource_id)
                if group.get("VpcId") != self.state["vpc_id"]:
                    raise ProxyError("Owned security group belongs to an unexpected VPC.")
        for port in (443, 8080):
            self.authorize("AuthorizeSecurityGroup", SecurityGroupId=self.state["security_group_id"],
                IpProtocol="tcp", PortRange=f"{port}/{port}", SourceCidrIp="0.0.0.0/0",
                Policy="accept", Priority=1, Description="proxy-fast", ClientToken=self.token(f"input-{port}"))
        self.authorize("AuthorizeSecurityGroupEgress", SecurityGroupId=self.state["security_group_id"],
            IpProtocol="all", PortRange="-1/-1", DestCidrIp="0.0.0.0/0", Policy="accept", Priority=1,
            Description="proxy-fast proxy egress", ClientToken=self.token("output"))

    def authorize(self, action, **params):
        try:
            self.cloud.call("ecs", action, **params)
        except ApiError as exc:
            # New normal security groups may already contain the identical allow-all egress rule.
            if exc.code not in ("InvalidPermission.Duplicate", "InvalidRule.Duplicate"):
                raise

    def ensure_switch(self, zone):
        slots = self.state.setdefault("switches", {})
        if zone not in slots:
            slots[zone] = {"cidr": f"10.77.{len(slots)}.0/24"}
            self.store.save()
        slot = slots[zone]
        value = self.resource("vswitch", slot["id"]) if slot.get("id") else None
        if not value:
            candidates = [s for s in self.resources("vswitch", owned=True)
                          if s.get("ZoneId") == zone and s.get("VpcId") == self.state["vpc_id"]]
            if len(candidates) > 1:
                raise ProxyError("Multiple owned switches in one zone; run off first.")
            if candidates:
                slot["id"] = candidates[0]["VSwitchId"]
            else:
                data = self.cloud.call("vpc", "CreateVSwitch", VpcId=self.state["vpc_id"], ZoneId=zone,
                    CidrBlock=slot["cidr"], VSwitchName="proxy-fast-"+self.state["owner_id"][:8],
                    ClientToken=self.token("switch-"+zone), **self.owner_params("vswitch"))
                slot["id"] = data["VSwitchId"]
            self.store.save()
        value = self._wait_network("vswitch", slot["id"])
        if value.get("VpcId") != self.state["vpc_id"] or value.get("ZoneId") != zone:
            raise ProxyError("Owned switch has unexpected VPC/zone.")
        return slot["id"]

    def adopt_instance(self):
        instance_id = self.state.get("instance_id")
        if instance_id:
            value = self.resource("instance", instance_id)
            if value:
                if self.state.get("pending_run"):
                    self.checkpoint(pending_run=None)
                return value
        values = self.resources("instance", owned=True)
        if len(values) > 1:
            raise ProxyError("Multiple owned instances found. Use off to release them before creating another.")
        if values:
            self.checkpoint(instance_id=values[0]["InstanceId"], pending_run=None)
            return values[0]
        return None

    @staticmethod
    def ipv4(instance):
        values = instance.get("PublicIpAddress", {}).get("IpAddress", [])
        return values[0] if values else instance.get("EipAddress", {}).get("IpAddress")

    def wait_ready(self):
        deadline = self.clock() + self.settings.timeout
        seen, previous = False, None
        while self.clock() < deadline:
            instance = self.resource("instance", self.state["instance_id"])
            if instance:
                seen = True
                status = instance.get("Status")
                if status != previous:
                    self.log(f"Instance {self.state['instance_id']}: {status}"); previous = status
                ip = self.ipv4(instance)
                if status == "Running" and ip:
                    self.checkpoint(public_ip=ip)
                    result = self.probe(ip, self.state["subscription_token"], self.state["deployment_id"])
                    if result:
                        self.checkpoint(phase="ready", result=result, last_error=None)
                        return self.summary("ready", instance)
                if status == "Stopped":
                    raise ProxyError("Instance is stopped. Run on again to start it, or off to release it.")
            elif seen:
                self.checkpoint(phase="reclaimed", result=None)
                raise ProxyError("Instance disappeared during deployment. Run on to create a replacement, or off to clean up.")
            self.sleep(2)
        raise ProxyError("Timed out waiting for the node and public subscriptions. Instance is retained; "
                         "check /var/log/proxy-fast/bootstrap.log via ECS console, or run off to release it.")

    def on(self):
        if self.state.get("phase") == "deleting":
            raise ProxyError("Cleanup was interrupted. Finish off before creating another instance.")
        current = self.adopt_instance()
        if current:
            if current.get("InstanceChargeType") != "PostPaid" or current.get("SpotStrategy") not in ("SpotWithPriceLimit", "SpotAsPriceGo"):
                raise ProxyError("Owned instance is not a pay-as-you-go spot instance; refusing to alter it.")
            if not self.state.get("subscription_token") or not self.state.get("deployment_id"):
                raise ProxyError("Found an owned instance but local deployment secrets are missing. Run off before on.")
            if current.get("Status") == "Stopped":
                self.cloud.call("ecs", "StartInstance", InstanceId=current["InstanceId"])
            return self.wait_ready()
        # A previous recorded instance was reclaimed/deleted. Preserve dedicated network.
        if self.state.get("instance_id"):
            self.checkpoint(instance_id=None, pending_run=None, result=None, subscription_token=None,
                            deployment_id=None, phase="reclaimed")
        if self.state.get("phase") == "deleting":
            raise ProxyError("Cleanup was interrupted. Finish off before creating another instance.")
        if self.state.get("pending_run"):
            self.log("Retrying the exact pending create request with its saved ClientToken.")
            self._run_pending()
            return self.wait_ready()
        choices = self.inventory()
        image_id, disk_size = self.image()
        if disk_size > 2048:
            raise ProxyError("Selected image requires more than the supported 2048 GiB system disk limit.")
        if not self.bootstrap.is_file():
            raise ProxyError(f"Missing bundled bootstrap script: {self.bootstrap}")
        bootstrap = self.bootstrap.read_bytes()
        self.checkpoint(phase="network", subscription_token=secrets.token_urlsafe(32),
                        deployment_id=uuid.uuid4().hex, result=None, image_id=image_id)
        user_data = make_user_data(bootstrap, self.state["subscription_token"], self.state["deployment_id"])
        self.ensure_network()
        last_error = None
        for zone, instance_type in choices:
            disk_category = self.disk_category(zone, instance_type)
            if not disk_category:
                self.log(f"Skipping {zone} / {instance_type}: no supported in-stock system disk.")
                continue
            switch = self.ensure_switch(zone)
            request = {"Amount": 1, "MinAmount": 1, "InstanceChargeType": "PostPaid",
                "SpotStrategy": "SpotWithPriceLimit", "SpotPriceLimit": self.settings.price_limit,
                "SpotDuration": self.settings.protection_hours, "SpotInterruptionBehavior": "Terminate",
                "ImageId": image_id, "InstanceType": instance_type, "ZoneId": zone, "VSwitchId": switch,
                "SecurityGroupId": self.state["security_group_id"], "SystemDisk.Category": disk_category,
                "SystemDisk.Size": disk_size, "InternetChargeType": "PayByTraffic",
                "InternetMaxBandwidthOut": self.settings.bandwidth, "IoOptimized": "optimized",
                "HttpEndpoint": "enabled", "HttpTokens": "required", "DeletionProtection": False,
                "InstanceName": "proxy-fast-"+self.state["owner_id"][:8], "UserData": user_data,
                "ClientToken": uuid.uuid4().hex, **self.owner_params("instance")}
            if instance_type.startswith(("ecs.t5", "ecs.t6")):
                request["CreditSpecification"] = "Standard"
            self.checkpoint(phase="creating", pending_run=request)
            self.log(f"Trying {zone} / {instance_type}; compute bid cap {self.settings.price_limit}/hour (account currency).")
            try:
                self._run_pending()
                return self.wait_ready()
            except ApiError as exc:
                # Only a definite capacity/compatibility rejection permits another token.
                if not capacity_rejected(exc):
                    raise
                last_error = exc
                self.checkpoint(pending_run=None, phase="network")
                self.log(f"Candidate unavailable: {exc.code}")
        raise ProxyError(f"No candidate could be created within the compute bid cap. Last error: {last_error}. "
                         "Dedicated network is retained; run off to remove it or adjust --price-limit and retry on.")

    def _run_pending(self):
        data = self.cloud.call("ecs", "RunInstances", **self.state["pending_run"])
        ids = data.get("InstanceIdSets", {}).get("InstanceIdSet", [])
        if len(ids) != 1:
            raise ProxyError("RunInstances returned no unique instance ID. Request retained; retry on, never create a second request manually.")
        self.checkpoint(instance_id=ids[0], phase="booting", pending_run=None)

    def summary(self, status, instance=None):
        result = self.state.get("result") or {}
        return {"status": status, "region": self.state.get("region", self.cloud.region),
            "instance_id": instance.get("InstanceId") if instance else self.state.get("instance_id"),
            "instance_status": instance.get("Status") if instance else None,
            "public_ip": self.ipv4(instance) if instance else None,
            "subscriptions": {k: result[k] for k in ("mihomo", "sing_box") if status == "ready" and k in result},
            "state_file": str(self.store.file), "last_error": self.state.get("last_error")}

    def status(self):
        current = self.adopt_instance()
        if not current:
            status = ("reclaimed" if self.state.get("instance_id") else
                      "cleanup-pending" if self.state.get("phase") == "deleting" else
                      "creation-uncertain" if self.state.get("pending_run") else
                      "incomplete" if self.state.get("vpc_id") or self.state.get("phase") == "network" else "off")
            return self.summary(status)
        if current.get("Status") != "Running":
            return self.summary(current.get("Status", "unknown").lower(), current)
        ip = self.ipv4(current)
        if ip and self.state.get("subscription_token") and self.state.get("deployment_id"):
            result = self.probe(ip, self.state["subscription_token"], self.state["deployment_id"])
            if result:
                self.checkpoint(phase="ready", result=result, last_error=None)
                return self.summary("ready", current)
        return self.summary("unhealthy" if self.state.get("phase") == "ready" else "provisioning", current)

    def _delete_and_wait(self, kind, resource):
        self.assert_owned(resource, kind)
        resource_id = resource[self.KINDS[kind][4]]
        self.log(f"Releasing owned {kind}: {resource_id}")
        deadline = self.clock() + self.settings.timeout
        while self.clock() < deadline:
            current = self.resource(kind, resource_id)
            if not current:
                return
            try:
                if kind == "instance":
                    if current.get("InstanceChargeType") != "PostPaid":
                        raise ProxyError("Refusing to release a subscription-billed instance.")
                    self.cloud.call("ecs", "DeleteInstance", InstanceId=resource_id, Force=True)
                elif kind == "security-group":
                    self.cloud.call("ecs", "DeleteSecurityGroup", SecurityGroupId=resource_id)
                elif kind == "vswitch":
                    self.cloud.call("vpc", "DeleteVSwitch", VSwitchId=resource_id)
                else:
                    self.cloud.call("vpc", "DeleteVpc", VpcId=resource_id, ClientToken=self.token("delete-"+resource_id))
            except ApiError as exc:
                if absent(exc):
                    return
                if not any(v in exc.code.lower() for v in ("incorrectstatus", "dependency", "inuse", "operationconflict", "taskconflict")):
                    raise
            self.sleep(2)
        raise ProxyError(f"Timed out releasing {kind} {resource_id}. State retained; rerun off.")

    def off(self):
        # Query tracked IDs too, so stripped/changed ownership tags cannot be hidden by a tag filter.
        tracked = [("instance", self.state.get("instance_id")), ("security-group", self.state.get("security_group_id")),
                   ("vpc", self.state.get("vpc_id"))]
        tracked += [("vswitch", value.get("id")) for value in self.state.get("switches", {}).values()]
        for kind, resource_id in tracked:
            if resource_id:
                self.resource(kind, resource_id)  # Fail closed on mismatched tags before deleting anything.
        if self.state.get("pending_run"):
            # A lost create response must be resolved before its VPC/state is discarded.
            # Reuse exactly the original request, never issue a new creation token.
            current = self.adopt_instance()
            if current:
                self.checkpoint(pending_run=None)
            else:
                self.log("Resolving an uncertain create request with its original ClientToken before cleanup.")
                try:
                    self._run_pending()
                except ApiError as exc:
                    if not capacity_rejected(exc):
                        raise
                    self.checkpoint(pending_run=None)
                if self.state.get("instance_id"):
                    deadline = self.clock() + self.settings.timeout
                    while not self.resource("instance", self.state["instance_id"]):
                        if self.clock() >= deadline:
                            raise ProxyError("Created instance not yet visible; state retained. Rerun off.")
                        self.sleep(2)
        self.checkpoint(phase="deleting")
        for kind in ("instance", "security-group", "vswitch", "vpc"):
            resources = self.resources(kind, owned=True)
            for resource in resources:
                self._delete_and_wait(kind, resource)
        # Recheck after asynchronous deletion. Never discard the recovery record early.
        if any(self.resources(kind, owned=True) for kind in self.KINDS):
            raise ProxyError("Owned resources still exist. State retained; rerun off.")
        self.store.clear_resources()
        return self.summary("off")
