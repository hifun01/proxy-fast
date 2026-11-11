"""Alibaba Cloud RPC transport. Credentials never leave the controller."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path


class ProxyError(Exception):
    """Actionable error safe to show without dumping request parameters."""


class ApiError(ProxyError):
    def __init__(self, code: str, message: str, request_id: str = ""):
        self.code, self.message, self.request_id = code, message, request_id
        super().__init__(f"{code}: {message}" + (f" [request {request_id}]" if request_id else ""))


class TransportError(ProxyError):
    pass


@dataclass(repr=False)
class Credentials:
    access_key_id: str = field(repr=False)
    access_key_secret: str = field(repr=False)
    security_token: str = field(default="", repr=False)


def load_credentials(profile: str | None = None, env=None, config_path: Path | None = None) -> Credentials:
    env = os.environ if env is None else env
    key = env.get("ALIBABA_CLOUD_ACCESS_KEY_ID", "")
    secret = env.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "")
    if key or secret:
        if not (key and secret):
            raise ProxyError("Set both ALIBABA_CLOUD_ACCESS_KEY_ID and ALIBABA_CLOUD_ACCESS_KEY_SECRET.")
        return Credentials(key, secret, env.get("ALIBABA_CLOUD_SECURITY_TOKEN", ""))
    path = config_path or Path.home() / ".aliyun" / "config.json"
    if path.is_file():
        try:
            config = json.loads(path.read_text(encoding="utf-8-sig"))
            name = profile or env.get("ALIBABA_CLOUD_PROFILE") or config.get("current", "default")
            item = next((p for p in config.get("profiles", []) if p.get("name") == name), None)
            if item and item.get("mode") in ("AK", "StsToken"):
                key, secret = item.get("access_key_id"), item.get("access_key_secret")
                token = item.get("sts_token") or item.get("security_token", "")
                if key and secret and (item.get("mode") != "StsToken" or token):
                    return Credentials(key, secret, token)
        except (ValueError, OSError, TypeError) as exc:
            raise ProxyError("Cannot read the Alibaba CLI credentials file.") from exc
        raise ProxyError("Selected Alibaba CLI profile must use AK or StsToken credentials.")
    raise ProxyError("No Alibaba credentials. Set ALIBABA_CLOUD_ACCESS_KEY_ID / "
                     "ALIBABA_CLOUD_ACCESS_KEY_SECRET, or configure an Alibaba CLI AK/STS profile once.")


def percent(value) -> str:
    if isinstance(value, bool):
        value = "true" if value else "false"
    return urllib.parse.quote(str(value), safe="~")


def signed_body(parameters: dict, credentials: Credentials, timestamp=None, nonce=None) -> bytes:
    params = {k: ("true" if v else "false") if isinstance(v, bool) else str(v)
              for k, v in parameters.items() if v is not None}
    params.update(Format="JSON", AccessKeyId=credentials.access_key_id,
                  SignatureMethod="HMAC-SHA1", SignatureVersion="1.0",
                  Timestamp=timestamp or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  SignatureNonce=nonce or uuid.uuid4().hex)
    if credentials.security_token:
        params["SecurityToken"] = credentials.security_token
    canonical = "&".join(f"{percent(k)}={percent(params[k])}" for k in sorted(params))
    string_to_sign = "POST&%2F&" + percent(canonical)
    digest = hmac.new((credentials.access_key_secret + "&").encode(), string_to_sign.encode(), hashlib.sha1).digest()
    signature = base64.b64encode(digest).decode()
    return (canonical + "&Signature=" + percent(signature)).encode("ascii")


class Cloud:
    VERSIONS = {"ecs": "2014-05-26", "vpc": "2016-04-28", "sts": "2015-04-01"}
    NO_REGION = {"DescribeInstanceTypes", "DeleteInstance", "StartInstance", "GetCallerIdentity"}

    def __init__(self, credentials: Credentials, region: str, opener=None, sleep=time.sleep):
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,48}[a-z0-9]", region):
            raise ProxyError("Invalid Alibaba Cloud region ID.")
        self.credentials, self.region, self.sleep = credentials, region, sleep
        # Cloud lifecycle calls must not depend on the proxy being deleted.
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, service: str, action: str, **params) -> dict:
        version = self.VERSIONS[service]
        if action not in self.NO_REGION:
            params.setdefault("RegionId", self.region)
        params.update(Action=action, Version=version)
        endpoint = "https://sts.aliyuncs.com/" if service == "sts" else f"https://{service}.{self.region}.aliyuncs.com/"
        retry_safe = action.startswith(("Describe", "Get")) or "ClientToken" in params
        attempts = 3 if retry_safe else 1
        for attempt in range(attempts):
            request = urllib.request.Request(endpoint, data=signed_body(params, self.credentials),
                headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "proxy-fast/0.2.0"})
            try:
                with self.opener.open(request, timeout=30) as response:
                    data = json.load(response)
                if "Code" in data:
                    raise ApiError(data["Code"], data.get("Message", "API request failed"), data.get("RequestId", ""))
                return data
            except urllib.error.HTTPError as exc:
                try:
                    data = json.loads(exc.read(65536))
                    error = ApiError(data.get("Code", f"HTTP{exc.code}"),
                                     data.get("Message", "API request failed"), data.get("RequestId", ""))
                except (ValueError, UnicodeError):
                    error = ApiError(f"HTTP{exc.code}", "Alibaba Cloud API returned an HTTP error")
                transient = exc.code >= 500 or exc.code == 429 or error.code.lower().startswith("throttl")
                if transient and attempt + 1 < attempts:
                    self.sleep(2 ** attempt); continue
                raise error from None
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                if attempt + 1 < attempts:
                    self.sleep(2 ** attempt); continue
                raise TransportError(f"{action}: network response uncertain. Rerun the same command; "
                                     "saved idempotency tokens prevent duplicate creates.") from exc
        raise AssertionError("unreachable")

    def account_id(self) -> str:
        account = str(self.call("sts", "GetCallerIdentity").get("AccountId", ""))
        if not account.isdigit():
            raise ProxyError("STS GetCallerIdentity did not return an account ID.")
        return account
