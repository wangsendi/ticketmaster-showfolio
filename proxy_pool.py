"""OneSocks node selection; upstream credentials stay in memory."""
from dataclasses import dataclass, field
from contextlib import contextmanager
import json
import ipaddress
import random
from pathlib import Path
import threading
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener


DEFAULT_PROXY_API = "http://127.0.0.1:10802/api/nodes"
NETWORK_MODE = "random_onesocks_proxy"


class NoAlternativeProxy(ValueError):
    pass


class ProxyRefreshError(ValueError):
    pass


@dataclass(frozen=True)
class ProxySelection:
    node: dict
    upstream: dict = field(repr=False)


class ProxyPool:
    def __init__(self, nodes, host, port, password):
        self.nodes = nodes
        self.host = host
        self.port = port
        self.password = password
        self.remaining = []
        self.previous_id = None
        self.random = random.SystemRandom()
        self.lock = threading.Lock()
        self.available = threading.Condition(self.lock)
        self.leased = set()
        self.refreshing = False
        self.refresh_error = None

    @classmethod
    def from_api(cls, nodes_url):
        try:
            source = urlsplit(nodes_url)
            valid = (source.scheme in ("http", "https") and source.hostname
                     and not source.username and not source.password
                     and not source.query and not source.fragment
                     and source.path.rstrip("/").endswith("/api/nodes"))
            if source.port is not None and not 1 <= source.port <= 65535:
                valid = False
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("--proxy-api 必须为不含凭据或查询参数的 HTTP(S) /api/nodes 地址。")
        config_url = urlunsplit((source.scheme, source.netloc,
                                source.path.rstrip("/").rsplit("/", 1)[0] + "/config", "", ""))
        # The management API must not depend on ambient HTTP_PROXY settings.
        opener = build_opener(ProxyHandler({}))

        def get_json(url):
            try:
                request = Request(url, headers={"Accept": "application/json"})
                with opener.open(request, timeout=10) as response:
                    value = json.load(response)
            except (URLError, OSError, ValueError):
                raise ValueError("无法读取代理 API；请检查 OneSocks 服务和接口地址。") from None
            if not isinstance(value, dict):
                raise ValueError("代理 API 返回格式无效。")
            return value

        payload = get_json(nodes_url)
        config = get_json(config_url)
        items = payload.get("items")
        if not isinstance(items, list):
            raise ValueError("代理 API 未返回 items 节点列表。")
        nodes, seen = [], set()
        for item in items:
            if not isinstance(item, dict):
                continue
            node_id, username = item.get("id"), item.get("username")
            if (not isinstance(node_id, str) or not node_id or node_id in seen
                    or not isinstance(username, str) or not 1 <= len(username.encode("utf-8")) <= 255):
                continue
            health = item.get("health", "unknown")
            if health in ("unhealthy", "dead", "down", "failed", "error", "unavailable", "disabled"):
                continue
            if item.get("disabled") is True:
                continue
            # Never retain the raw outbound configuration or its secrets.
            nodes.append({"id": node_id, "name": item.get("name", node_id),
                          "protocol": item.get("protocol"), "health": health, "username": username})
            # Retain only the management API's safe IP-check fields.
            check = item.get("ip_check")
            if isinstance(check, dict) and not check.get("error"):
                data = check.get("data")
                info = data.get("ip_info") if isinstance(data, dict) else None
                if isinstance(info, dict):
                    country, address = info.get("country_code"), info.get("ip")
                    try:
                        if not isinstance(country, str) or len(country.strip()) != 2:
                            raise ValueError()
                        if not isinstance(address, str):
                            raise ValueError()
                        ipaddress.ip_address(address)
                    except (ValueError, TypeError):
                        pass
                    else:
                        nodes[-1]["reported_exit"] = {"country":country.strip().upper(), "ip":address,
                                                     "checkedAtUTC":check.get("checked_at")}
            seen.add(node_id)
        if not nodes:
            raise ValueError("代理 API 没有可选择的节点；未启动浏览器。")
        listen = config.get("socks_listen")
        try:
            address = urlsplit("socks5://" + listen) if isinstance(listen, str) else None
            if (address is None or not address.port or address.path or address.query or address.fragment
                    or address.username is not None or address.password is not None):
                raise ValueError()
            host, port = address.hostname, address.port
            if not host or host in ("0.0.0.0", "::", "127.0.0.1", "::1", "localhost"):
                host = source.hostname
        except ValueError:
            raise ValueError("代理 API 的 socks_listen 地址无效。") from None
        # OneSocks accepts any nonempty password when no shared password is set.
        password = config.get("socks_password") or "anything"
        if not isinstance(password, str) or not 1 <= len(password.encode("utf-8")) <= 255:
            raise ValueError("代理 API 的 SOCKS5 密码格式无效。")
        return cls(nodes, host, port, password)

    def restrict(self, report_path, nodes_url, campaign_url):
        """Limit selection to a completed screening report, intersected with current nodes."""
        try:
            report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise ValueError("无法读取代理筛选报告，请先运行 proxy_check.py。") from None
        if (not isinstance(report, dict) or report.get("version") != 1 or report.get("complete") is not True
                or not isinstance(report.get("nodesAPI"), str)
                or report.get("nodesAPI", "").rstrip("/") != nodes_url.rstrip("/")
                or report.get("campaignURL") != campaign_url):
            raise ValueError("代理筛选报告尚未完成，或接口/活动地址与本次运行不一致。")
        ids = report.get("nodeIds")
        if not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids):
            raise ValueError("代理筛选报告没有通过的节点，请重新检测。")
        selected_ids = set(ids)
        nodes = [node for node in self.nodes if node["id"] in selected_ids]
        if not nodes:
            raise ValueError("筛选通过的节点已不在当前代理列表中，请重新检测。")
        exits = {}
        for result in report.get("results") or []:
            if isinstance(result, dict) and result.get("accepted") and isinstance(result.get("node"), dict):
                location = result.get("exitAfterCampaign") or result.get("exit") or {}
                if isinstance(location, dict) and isinstance(location.get("ip"), str):
                    exits[result["node"].get("id")] = location["ip"]
        self.nodes = [{**node, "screening": {"country": report.get("country"), "exitIP": exits.get(node["id"]),
                       "checkedAtUTC": report.get("finishedAtUTC", report.get("checkedAtUTC")),
                       "campaignURL": campaign_url}} for node in nodes]
        self.remaining = []
        self.previous_id = None

    def select(self):
        """Shuffle a full cycle, avoiding immediate reuse across cycle boundaries."""
        with self.lock:
            return self._select(self.leased)

    @contextmanager
    def lease(self, exclude_ids=(), stop_event=None, wait=False, previous=None):
        """Keep the selected node exclusive until its account attempt finishes."""
        with self.available:
            while True:
                if stop_event and stop_event.is_set():
                    raise InterruptedError("查询已取消。")
                if self.refreshing:
                    self.available.wait(timeout=0.2)
                    continue
                if self.refresh_error:
                    raise ProxyRefreshError(self.refresh_error)
                excluded = set(exclude_ids) | (self.retry_exclusions(previous) if previous else set())
                if not any(node["id"] not in excluded for node in self.nodes):
                    raise NoAlternativeProxy("没有其他可用的代理出口，已停止自动重试。")
                if any(node["id"] not in excluded | self.leased for node in self.nodes):
                    break
                if not wait:
                    raise ValueError("代理节点均在使用中，请降低并发数。")
                self.available.wait(timeout=0.2)
            selected = self._select(self.leased | excluded)
            self.leased.add(selected.node["id"])
        try:
            yield selected
        finally:
            with self.available:
                self.leased.remove(selected.node["id"])
                self.available.notify_all()

    def begin_refresh(self):
        with self.available:
            self.refreshing = True
            self.refresh_error = None

    def finish_refresh(self, replacement=None, error=None):
        """Replace future routes while keeping leases already in use intact."""
        with self.available:
            self.nodes = replacement.nodes if replacement else []
            if replacement:
                self.host, self.port, self.password = replacement.host, replacement.port, replacement.password
            self.remaining = []
            self.refresh_error = error
            self.refreshing = False
            self.available.notify_all()

    def retry_exclusions(self, previous):
        """Skip the previous route and known routes with the same screened exit IP."""
        address = previous.node.get("screening", {}).get("exitIP")
        return {node["id"] for node in self.nodes if node["id"] == previous.node["id"]
                or address and node.get("screening", {}).get("exitIP") == address}

    def _select(self, excluded):
        available = [index for index, node in enumerate(self.remaining) if node["id"] not in excluded]
        if not available:
            self.remaining = [node for node in self.nodes if node["id"] not in excluded]
            if not self.remaining:
                raise ValueError("代理节点均在使用中，请降低并发数。")
            self.random.shuffle(self.remaining)
            if len(self.remaining) > 1 and self.remaining[-1]["id"] == self.previous_id:
                self.remaining[0], self.remaining[-1] = self.remaining[-1], self.remaining[0]
            available = list(range(len(self.remaining)))
        selected = self.remaining.pop(available[-1])
        self.previous_id = selected["id"]
        return ProxySelection(
            node={key: selected[key] for key in ("id", "name", "protocol", "health", "screening") if key in selected},
            upstream={"socksHost": self.host, "socksPort": self.port,
                      "username": selected["username"], "password": self.password,
                      "maxAttempts": 1},
        )
