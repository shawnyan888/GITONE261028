#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ONES 缺陷属性批量修改脚本（单文件实现，实施依据 ONES/plan-a.md）。

用法:
    python update_bug.py --bug FUCO-7 FUCO-4 --leixing "安全问题" --chengdu "致命" \
        --lingling "fusion2026.1.100.01" --force --yes

环境变量:
    ONES_BUG     ONES 里任意一个 issue 页面 URL（用于提取 host / teamID / project）
    ONES_API     API key / token
    ONES_USER_ID 可选，仅认证方式 3 使用

退出码:
    0  成功 / DRY-RUN 正常结束 / 无任何字段变化
    1  校验失败（取值不在候选列表、属性名定位不到、issue 解析不到）
    2  参数或环境变量错误
    3  网络 / 认证 / API 错误
"""

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from urllib.parse import quote

try:
    import requests
except ImportError:  # pragma: no cover - 环境缺失依赖时给出明确提示
    print("[错误] 缺少依赖 requests，请先执行: pip install requests")
    sys.exit(2)


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_VERSION = "1.0.0"
CACHE_FILENAME = "ones_field_cache.json"
LOG_FILENAME = "update_bug.log"

API_PREFIX = "/openapi/v2"
MAX_RETRIES = 3
BACKOFF_SECONDS = (1, 2, 4)
MAX_PAGES = 100

AUTH_BEARER = "Authorization: Bearer"
AUTH_TOKEN = "Ones-Auth-Token"
AUTH_TOKEN_USER = "Ones-Auth-Token+Ones-User-Id"

# (缓存键, 目标属性名候选, 对应命令行参数)
FIELD_SPECS = (
    ("leixing", ("缺陷类型",), "--leixing"),
    ("chengdu", ("严重程度",), "--chengdu"),
    ("lingling", ("ling-build-version",), "--lingling"),
)

BUG_ARG_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-[0-9]+$")


# --------------------------------------------------------------------------
# 异常定义
# --------------------------------------------------------------------------
class UsageError(Exception):
    """参数或环境变量错误，映射退出码 2。"""


class ValidationError(Exception):
    """取值 / 属性 / issue 解析校验失败，映射退出码 1。"""


class FieldError(ValidationError):
    """属性定位失败或多义，携带可选属性清单便于人工确认。"""

    def __init__(self, message, available=None):
        super().__init__(message)
        self.available = available or []


class OptionError(ValidationError):
    """取值不在单选候选列表中，携带合法值清单。"""

    def __init__(self, message, options=None):
        super().__init__(message)
        self.options = options or []


class ApiError(Exception):
    """网络 / 认证 / 业务错误，映射退出码 3。"""

    def __init__(self, status, code, message, body=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.body = body

    def describe(self):
        parts = []
        if self.status is not None:
            parts.append("HTTP %s" % self.status)
        if self.code:
            parts.append("errorCode=%s" % self.code)
        parts.append("errorMsg=%s" % (self.message or ""))
        text = " ".join(parts)
        if self.body and self.code in ("AUTH_FAILED", "ENDPOINT_NOT_FOUND"):
            text += "\n响应体: %s" % self.body
        return text


# --------------------------------------------------------------------------
# 基础设施：日志、字符串工具
# --------------------------------------------------------------------------
class Logger:
    """追加写 ONES/update_bug.log，token 永不落盘。"""

    def __init__(self, path):
        self.path = path

    def write(self, level, message):
        stamp = datetime.now().isoformat(timespec="seconds")
        line = "%s [%s] %s\n" % (stamp, level, message)
        try:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass

    def info(self, message):
        self.write("INFO", message)

    def debug(self, message):
        self.write("DEBUG", message)

    def warn(self, message):
        self.write("WARN", message)

    def error(self, message):
        self.write("ERROR", message)


def mask(secret):
    """token 脱敏：仅保留前 4 位与后 4 位。"""
    if not secret:
        return "(空)"
    if len(secret) <= 12:
        return secret[:2] + "****"
    return "%s****%s" % (secret[:4], secret[-4:])


def normalize(value):
    """NFKC 归一化 + 去空白 + 忽略大小写，用于取值比较。"""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    return text.strip().casefold()


def disp_width(text):
    """按中文宽度算显示列宽。"""
    width = 0
    for char in str(text):
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def pad(text, width):
    """右侧补空格到指定显示列宽。"""
    text = str(text)
    return text + " " * max(0, width - disp_width(text))


def value_label(value):
    """把 fieldValues[].value 归一化成可读标签。"""
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("value", "name", "text", "title", "label"):
            if value.get(key) is not None:
                return str(value[key])
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return ", ".join(value_label(item) for item in value)
    return str(value)


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 参数与环境变量
# --------------------------------------------------------------------------
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="批量修改 ONES issue 的缺陷类型 / 严重程度 / ling-build-version（默认仅预览）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python update_bug.py --bug FUCO-7 FUCO-4 --leixing 安全问题 --chengdu 致命 \\\n"
            "      --lingling fusion2026.1.100.01 --force --yes\n\n"
            "环境变量: ONES_BUG（issue 页面 URL）、ONES_API（API key）、ONES_USER_ID（可选）\n"
            "退出码: 0 成功 / 1 校验失败 / 2 参数或环境错误 / 3 网络或 API 错误"
        ),
    )
    parser.add_argument("--bug", nargs="+", required=True, metavar="KEY",
                        help="一个或多个 issue key，例如 FUCO-7 FUCO-4")
    parser.add_argument("--leixing", metavar="VALUE", help="缺陷类型（单选，值必须是合法选项）")
    parser.add_argument("--chengdu", metavar="VALUE", help="严重程度（单选，值必须是合法选项）")
    parser.add_argument("--lingling", metavar="VALUE", help="ling-build-version 属性值")
    parser.add_argument("--force", action="store_true",
                        help="强制重建 ones_field_cache.json（含重新探测认证头）")
    parser.add_argument("--yes", action="store_true", help="真正执行修改；缺省为 DRY-RUN")
    parser.add_argument("--timeout", type=int, default=30, help="单次 HTTP 超时秒数，默认 30")
    parser.add_argument("--debug", action="store_true", help="打印请求方法 / 路径 / 状态码")
    return parser.parse_args(argv)


def load_env():
    """读取必需环境变量，缺失时抛 UsageError。"""
    bug = (os.environ.get("ONES_BUG") or "").strip()
    api_key = (os.environ.get("ONES_API") or "").strip()
    user_id = (os.environ.get("ONES_USER_ID") or "").strip() or None

    missing = []
    if not bug:
        missing.append("ONES_BUG")
    if not api_key:
        missing.append("ONES_API")
    if missing:
        raise UsageError(
            "缺少环境变量 %s。设置示例（PowerShell）:\n"
            "  $env:ONES_BUG = \"https://ones.cn/project/#/team/KKv1z9x9/project/FUCO/.../issue/FUCO-7\"\n"
            "  $env:ONES_API = \"<你的 API key>\""
            % "、".join(missing)
        )
    return {"ones_bug": bug, "ones_api": api_key, "ones_user_id": user_id}


def check_bug_args(bug_keys, url_info):
    """校验 --bug 取值格式与项目前缀，返回去重后的有序列表。"""
    targets = []
    seen = set()
    for raw in bug_keys:
        key = (raw or "").strip()
        if not BUG_ARG_PATTERN.match(key):
            raise UsageError("issue key 格式非法: %r，期望形如 FUCO-7" % raw)
        if key not in seen:
            seen.add(key)
            targets.append(key)
    if len(targets) != len(bug_keys):
        print("[提示] --bug 存在重复项，已去重，共 %d 个" % len(targets))
    if url_info["project_ref_kind"] == "short_name":
        prefix = url_info["project_ref"].casefold()
        for key in targets:
            key_prefix = key.rsplit("-", 1)[0]
            if key_prefix.casefold() != prefix:
                raise UsageError(
                    "issue %s 的项目前缀与 ONES_BUG 中的项目 %s 不一致"
                    % (key, url_info["project_ref"])
                )
    return targets


def collect_change_args(args):
    """收集本次要修改的属性（缓存键 -> 用户输入值）。"""
    wanted = {}
    if args.leixing is not None:
        wanted["leixing"] = args.leixing
    if args.chengdu is not None:
        wanted["chengdu"] = args.chengdu
    if args.lingling is not None:
        wanted["lingling"] = args.lingling
    if not wanted:
        raise UsageError(
            "至少提供一个要修改的属性：--leixing / --chengdu / --lingling"
        )
    return wanted


# --------------------------------------------------------------------------
# URL 解析
# --------------------------------------------------------------------------
def parse_ones_url(url):
    """从 ONES_BUG 提取 host / teamID / project / issueKey。"""
    host_match = re.match(r"^(https?://[^/]+)", url)
    team_match = re.search(r"/team/([^/?#]+)", url)
    project_match = re.search(r"/project/([^/?#]+)", url)
    issue_match = re.search(r"/issue/([^/?#]+)", url)

    if not host_match or not team_match or not project_match:
        raise UsageError(
            "无法从 ONES_BUG 解析 host / team / project，请检查 URL:\n  %s\n"
            "期望形如: https://ones.cn/project/#/team/KKv1z9x9/project/FUCO/.../issue/FUCO-7"
            % url
        )

    project_ref = project_match.group(1)
    kind = "uuid" if len(project_ref) >= 16 and re.fullmatch(r"[0-9A-Za-z]+", project_ref) else "short_name"

    issue_key = issue_match.group(1) if issue_match else url.rstrip("/").rsplit("/", 1)[-1]
    if not BUG_ARG_PATTERN.match(issue_key):
        issue_key = None

    return {
        "host": host_match.group(1),
        "team_id": team_match.group(1),
        "project_ref": project_ref,
        "project_ref_kind": kind,
        "issue_key": issue_key,
    }


# --------------------------------------------------------------------------
# API 客户端
# --------------------------------------------------------------------------
class ApiClient:
    """封装 {host}/openapi/v2，负责认证头探测、重试与错误归一化。"""

    def __init__(self, host, token, user_id=None, timeout=30, debug=False, logger=None):
        self.base = host.rstrip("/") + API_PREFIX
        self.host = host.rstrip("/")
        self.token = token
        self.user_id = user_id
        self.timeout = max(5, int(timeout or 30))
        self.debug = debug
        self.logger = logger or Logger(os.path.join(SCRIPT_DIR, LOG_FILENAME))
        self.auth_header = None
        self.session = requests.Session()

    # -- 请求头 ------------------------------------------------------------
    def build_headers(self, auth_header=None):
        mode = auth_header or self.auth_header or AUTH_TOKEN
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if mode == AUTH_BEARER:
            headers["Authorization"] = "Bearer %s" % self.token
        elif mode == AUTH_TOKEN_USER:
            headers["Ones-Auth-Token"] = self.token
            if self.user_id:
                headers["Ones-User-Id"] = self.user_id
        else:
            headers["Ones-Auth-Token"] = self.token
        return headers

    # -- 统一请求 ----------------------------------------------------------
    def request(self, method, path, params=None, body=None, auth_header=None):
        url = self.base + path
        attempt = 0
        while True:
            attempt += 1
            if self.debug:
                print("[debug] %s %s params=%s" % (method, path, params or {}))
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    headers=self.build_headers(auth_header),
                    params=params,
                    json=body,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                self.logger.debug("%s %s 网络异常(第 %d 次): %s" % (method, path, attempt, exc))
                if attempt <= MAX_RETRIES:
                    time.sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])
                    continue
                raise ApiError(None, "NETWORK_ERROR", str(exc))

            status = response.status_code
            if self.debug:
                print("[debug] %s %s -> HTTP %s" % (method, path, status))
            self.logger.debug("%s %s HTTP %s" % (method, path, status))

            data = None
            if response.content:
                try:
                    data = response.json()
                except ValueError:
                    data = None

            if status == 429 or 500 <= status < 600:
                if attempt <= MAX_RETRIES:
                    self.logger.info("%s %s 返回 %s，第 %d 次重试" % (method, path, status, attempt))
                    time.sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])
                    continue

            if status >= 400:
                code = None
                message = response.reason
                if isinstance(data, dict):
                    code = data.get("errorCode")
                    message = data.get("errorMsg") or message
                elif response.text:
                    message = response.text[:300]
                raise ApiError(status, code or ("HTTP_%d" % status), message, data)

            if isinstance(data, dict) and data.get("result") == "FAIL":
                raise ApiError(status, data.get("errorCode"), data.get("errorMsg"), data)

            return data if data is not None else {}

    # -- 认证探测 ----------------------------------------------------------
    def probe_auth(self, team_id):
        """按 Authorization Bearer -> Ones-Auth-Token -> +Ones-User-Id 顺序探测。"""
        candidates = [AUTH_BEARER, AUTH_TOKEN]
        if self.user_id:
            candidates.append(AUTH_TOKEN_USER)

        last_error = None
        for mode in candidates:
            try:
                self.request("GET", "/project/issueFields",
                             params={"teamID": team_id, "limit": 1}, auth_header=mode)
            except ApiError as exc:
                if exc.status == 404:
                    raise ApiError(404, "ENDPOINT_NOT_FOUND",
                                   "openapi 前缀或 host 不正确，请检查 ONES_BUG（%s）" % self.host,
                                   exc.body)
                if exc.status is None or (exc.status is not None and exc.status >= 500):
                    raise
                last_error = exc
                continue
            self.logger.info("认证方式命中: %s" % mode)
            return mode

        detail = last_error.describe() if last_error else "无更多信息"
        raise ApiError(None, "AUTH_FAILED",
                       "三种认证方式均失败，最后一次: %s" % detail,
                       getattr(last_error, "body", None))

    # -- issue key 是否可直接作为 issueID ---------------------------------
    def probe_issue_key(self, team_id, issue_key):
        if not issue_key:
            return False
        try:
            data = self.request("GET", "/project/issues/%s" % quote(issue_key, safe=""),
                                params={"teamID": team_id})
        except ApiError as exc:
            if exc.status in (400, 404):
                self.logger.info("issue key 不能直接作为 issueID（HTTP %s），改为翻页解析" % exc.status)
                return False
            raise
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        return bool(payload.get("id"))


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------
def cache_path():
    return os.path.join(SCRIPT_DIR, CACHE_FILENAME)


def load_cache():
    """读取缓存；不存在或损坏时返回 None。"""
    path = cache_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        print("[警告] 缓存 %s 无法解析（%s），将重新构建" % (CACHE_FILENAME, exc))
        return None
    if not isinstance(data, dict) or "fields" not in data or "_meta" not in data:
        print("[警告] 缓存 %s 结构不完整，将重新构建" % CACHE_FILENAME)
        return None
    data.setdefault("issues", {})
    return data


def save_cache(cache):
    path = cache_path()
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, ensure_ascii=False, indent=2)
    except OSError as exc:
        print("[警告] 写入缓存失败: %s" % exc)


def cache_matches(cache, url_info):
    meta = cache.get("_meta") or {}
    return (meta.get("host", "").rstrip("/") == url_info["host"].rstrip("/")
            and meta.get("team_id") == url_info["team_id"]
            and meta.get("project_ref") == url_info["project_ref"])


# --------------------------------------------------------------------------
# 分页与属性解析
# --------------------------------------------------------------------------
def paged_list(api, path, params, limit, max_pages=MAX_PAGES, allow_retry_without=None):
    """按 cursor 翻页拉取 data.list。allow_retry_without 为出错时可剔除的参数名。"""
    items = []
    cursor = None
    for _ in range(max_pages):
        query = dict(params)
        query["limit"] = limit
        if cursor:
            query["cursor"] = cursor
        try:
            data = api.request("GET", path, params=query)
        except ApiError as exc:
            if allow_retry_without and exc.status in (400, 422) and allow_retry_without in query:
                removed = query.pop(allow_retry_without)
                api.logger.warn("%s 拒绝参数 %s=%s，改为客户端过滤" % (path, allow_retry_without, removed))
                params = {k: v for k, v in params.items() if k != allow_retry_without}
                allow_retry_without = None
                continue
            raise
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        items.extend(payload.get("list") or [])
        page = payload.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
        if not cursor:
            break
    return items


def fetch_all_issue_fields(api, team_id):
    """拉取 team 下全部工作项属性（含单选项）。"""
    return paged_list(api, "/project/issueFields", {"teamID": team_id}, limit=500)


def field_names(field):
    names = [field.get("name") or ""]
    for alias in field.get("aliases") or []:
        names.append(str(alias))
    return names


def pick_field(fields, wanted_names):
    """按 完全相等 > 去空白相等 > 忽略大小写相等 > 别名 的优先级定位唯一属性。"""
    best_score = 0
    best = []
    targets = [normalize(name) for name in wanted_names]
    for field in fields:
        score = 0
        raw_name = field.get("name") or ""
        for target in wanted_names:
            if raw_name == target:
                score = max(score, 4)
        for alias in field.get("aliases") or []:
            if normalize(alias) in targets:
                score = max(score, 1)
        folded = normalize(raw_name)
        if folded in targets:
            score = max(score, 3 if raw_name.strip() != raw_name else 2)
        if score > best_score:
            best_score = score
            best = [field]
        elif score == best_score and score > 0:
            best.append(field)

    if best_score == 0:
        listing = ["%s (id=%s, type=%s)" % (f.get("name"), f.get("id"), f.get("typeEnum"))
                   for f in fields]
        raise FieldError("未找到属性 %s" % " / ".join(wanted_names), listing)
    if len(best) > 1:
        listing = ["%s (id=%s)" % (f.get("name"), f.get("id")) for f in best]
        raise FieldError("属性 %s 匹配到多个（%d 个），无法确定" % (" / ".join(wanted_names), len(best)),
                         listing)
    return best[0]


def option_entries(field):
    """把 options 归一化为 [{id, value}]，value 为可比较的标签字符串。"""
    entries = []
    for option in field.get("options") or []:
        raw = option.get("value")
        option_id = option.get("id")
        if isinstance(raw, dict):
            label = value_label(raw)
            submit_id = raw.get("id") or option_id
        elif raw is None:
            label = str(option_id or "")
            submit_id = option_id
        else:
            label = str(raw)
            submit_id = option_id
        entries.append({"id": submit_id, "value": label})
    return entries


def to_field_meta(field):
    type_enum = field.get("typeEnum")
    try:
        type_value = int(type_enum)
    except (TypeError, ValueError):
        type_value = 0
    return {
        "field_id": field.get("id"),
        "name": field.get("name"),
        "type_enum": type_value,
        "select_mode": type_value == 1,
        "multi": type_value == 16,
        "options": option_entries(field),
    }


def resolve_project_id(api, team_id, project_ref):
    """项目标识（或 uuid）-> project uuid。"""
    if len(project_ref) >= 16 and re.fullmatch(r"[0-9A-Za-z]+", project_ref):
        return project_ref
    projects = paged_list(api, "/project/projects", {"teamID": team_id}, limit=100)
    folded = normalize(project_ref)
    keys = ("name", "shortName", "short_name", "identifier", "key", "code")
    for project in projects:
        project_id = project.get("id")
        if not project_id:
            continue
        if normalize(project_id) == folded:
            return project_id
        for name_key in keys:
            if project.get(name_key) and normalize(project[name_key]) == folded:
                return project_id
    raise ValidationError("在 team %s 下未找到项目 %s" % (team_id, project_ref))


def build_cache(api, url_info):
    """构建 _meta 与 fields 段并落盘（等价于一次局部 --force）。"""
    team_id = url_info["team_id"]
    print("[ONES] 首次运行 / --force：正在探测认证方式 …")
    auth_header = api.probe_auth(team_id)
    api.auth_header = auth_header

    key_supported = api.probe_issue_key(team_id, url_info["issue_key"])

    project_id = None
    if url_info["project_ref_kind"] == "uuid":
        project_id = url_info["project_ref"]
    else:
        try:
            project_id = resolve_project_id(api, team_id, url_info["project_ref"])
        except ValidationError as exc:
            if not key_supported:
                raise
            api.logger.warn("%s（issue key 可直接使用，忽略）" % exc)

    fields = fetch_all_issue_fields(api, team_id)
    meta_fields = {}
    for cache_key, wanted_names, flag in FIELD_SPECS:
        field = pick_field(fields, wanted_names)
        meta = to_field_meta(field)
        meta_fields[cache_key] = meta
        api.logger.info("属性定位: %s -> %s (id=%s, type=%s, 选项 %d 个)"
                        % (flag, meta["name"], meta["field_id"], meta["type_enum"],
                           len(meta["options"])))

    cache = {
        "_meta": {
            "api_version": "v2",
            "host": url_info["host"].rstrip("/"),
            "team_id": team_id,
            "project_ref": url_info["project_ref"],
            "project_ref_kind": url_info["project_ref_kind"],
            "project_id": project_id,
            "auth_header": auth_header,
            "issue_key_supported": bool(key_supported),
            "generated_at": now_iso(),
            "script_version": SCRIPT_VERSION,
        },
        "fields": meta_fields,
        "issues": {},
    }
    save_cache(cache)
    print("[ONES] 缓存已生成: %s" % cache_path())
    return cache


# --------------------------------------------------------------------------
# issue 解析
# --------------------------------------------------------------------------
def find_issue_by_number(api, team_id, project_id, number):
    """在项目内翻页查找工作项编号，必要时退化为全量翻页 + 客户端过滤。"""
    params = {"teamID": team_id}
    if project_id:
        params["projectID"] = project_id
    issues = paged_list(api, "/project/issues", params, limit=100,
                        allow_retry_without="projectID")
    for issue in issues:
        if int(issue.get("number") or -1) != number:
            continue
        if project_id:
            issue_project = issue.get("project") or {}
            issue_project_id = issue_project.get("id") if isinstance(issue_project, dict) else None
            if issue_project_id and issue_project_id != project_id:
                continue
        return issue
    return None


def resolve_issue_id(api, cache, key):
    """issue key -> {id, number, project_id}，结果写入缓存。"""
    issues = cache.setdefault("issues", {})
    if key in issues and issues[key].get("id"):
        return issues[key]

    meta = cache["_meta"]
    team_id = meta["team_id"]
    record = None

    if meta.get("issue_key_supported"):
        try:
            data = api.request("GET", "/project/issues/%s" % quote(key, safe=""),
                               params={"teamID": team_id})
        except ApiError as exc:
            if exc.status in (400, 404):
                api.logger.warn("issue key %s 直接查询失败（HTTP %s），改用编号翻页" % (key, exc.status))
                meta["issue_key_supported"] = False
                save_cache(cache)
            else:
                raise
        else:
            payload = data.get("data") if isinstance(data.get("data"), dict) else data
            project = payload.get("project") if isinstance(payload.get("project"), dict) else {}
            record = {"id": payload.get("id"), "number": payload.get("number"),
                      "project_id": project.get("id")}

    if record is None:
        number = int(key.rsplit("-", 1)[1])
        project_id = meta.get("project_id")
        if not project_id and meta.get("project_ref_kind") != "uuid":
            project_id = resolve_project_id(api, team_id, meta.get("project_ref"))
            meta["project_id"] = project_id
        issue = find_issue_by_number(api, team_id, project_id, number)
        if not issue:
            raise ValidationError("在项目 %s 下未找到编号 %s 的工作项（issue %s）"
                                  % (meta.get("project_ref"), number, key))
        project = issue.get("project") if isinstance(issue.get("project"), dict) else {}
        record = {"id": issue.get("id"), "number": issue.get("number"),
                  "project_id": project.get("id") or project_id}

    if not record.get("id"):
        raise ValidationError("issue %s 解析结果缺少 id" % key)
    issues[key] = record
    save_cache(cache)
    return record


def get_current(api, team_id, issue_id, wanted_ids):
    """读取 issue 当前值，返回 {fieldID: 可读标签}。"""
    data = api.request("GET", "/project/issues/%s" % quote(issue_id, safe=""),
                       params={"teamID": team_id})
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    current = {}
    for item in payload.get("fieldValues") or []:
        field_id = item.get("fieldID")
        if field_id in wanted_ids:
            current[field_id] = value_label(item.get("value"))
    # 系统属性（defectType / severityLevel 等）可能不在 fieldValues 中，按 id 兜底匹配
    for value in payload.values():
        if isinstance(value, dict) and value.get("id") in wanted_ids and "value" in value:
            current.setdefault(value["id"], value_label(value.get("value")))
    return current


# --------------------------------------------------------------------------
# 取值校验与请求体
# --------------------------------------------------------------------------
def match_option(field, raw):
    folded = normalize(raw)
    for option in field.get("options") or []:
        if normalize(option.get("value")) == folded:
            return option
    for option in field.get("options") or []:
        if option.get("id") and normalize(option.get("id")) == folded:
            return option
    return None


def prepare_changes(wanted, cache_fields):
    """把命令行输入校验成可提交的变更项列表。"""
    changes = []
    for cache_key, wanted_names, flag in FIELD_SPECS:
        if cache_key not in wanted:
            continue
        field = cache_fields.get(cache_key)
        if not field:
            raise ValidationError("缓存中缺少 %s 的属性信息，请加 --force 重建" % flag)
        raw = wanted[cache_key]
        text = (raw or "").strip()
        if not text:
            raise ValidationError("%s 的值为空" % flag)

        if field.get("select_mode"):
            option = match_option(field, raw)
            if option is None:
                raise OptionError(
                    "%s 的值 %r 不在「%s」的候选列表中（共 %d 个）"
                    % (flag, raw, field.get("name"), len(field.get("options") or [])),
                    ["%s (id=%s)" % (o.get("value"), o.get("id")) for o in field.get("options") or []],
                )
            label = str(option.get("value"))
            submit = option.get("id") or option.get("value")
            submit_label = option.get("value")
        elif field.get("multi"):
            labels = [normalize(o.get("value")) for o in field.get("options") or []]
            option = match_option(field, raw)
            if labels and option is None:
                raise OptionError(
                    "%s 的值 %r 不在「%s」的候选列表中（多选但本脚本一次只写一个值）"
                    % (flag, raw, field.get("name")),
                    ["%s (id=%s)" % (o.get("value"), o.get("id")) for o in field.get("options") or []],
                )
            label = str(option.get("value")) if option else text
            submit = (option.get("id") or option.get("value")) if option else text
            submit_label = option.get("value") if option else text
        else:
            label = text
            submit = text
            submit_label = text

        changes.append({
            "cache_key": cache_key,
            "flag": flag,
            "field_id": field.get("field_id"),
            "field_name": field.get("name"),
            "type": field.get("type_enum") or 2,
            "multi": bool(field.get("multi")),
            "submit": submit,
            "submit_label": submit_label,
            "label": label,
        })
    return changes


def build_payload(changes, mode="id"):
    """组装 PUT body；mode=label 时用选项标签提交（值格式兜底）。"""
    items = []
    for change in changes:
        value = change["submit"] if mode == "id" else change["submit_label"]
        if change["multi"]:
            value = [value]
        items.append({"fieldID": change["field_id"], "type": change["type"], "value": value})
    return {"fieldValues": items}


def apply_update(api, team_id, issue_id, changes):
    """单个 issue 一次 PUT；选项 id 提交失败时用选项标签兜底重试一次。"""
    payload = build_payload(changes, mode="id")
    try:
        return api.request("PUT", "/project/issues/%s" % quote(issue_id, safe=""),
                           params={"teamID": team_id}, body=payload)
    except ApiError as exc:
        if exc.code in ("NETWORK_ERROR",) or exc.status in (401, 403, 404, 429) or (exc.status or 0) >= 500:
            raise
        api.logger.warn("issue %s 以选项 id 提交失败（%s），改用选项值重试一次" % (issue_id, exc.describe()))
        payload = build_payload(changes, mode="label")
        return api.request("PUT", "/project/issues/%s" % quote(issue_id, safe=""),
                           params={"teamID": team_id}, body=payload)


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
def print_header(url_info, cache, cache_state):
    meta = cache["_meta"]
    project_id = meta.get("project_id") or "-"
    print("[ONES] host=%s team=%s project=%s(%s) cache=%s"
          % (url_info["host"], meta.get("team_id"), meta.get("project_ref"), project_id, cache_state))
    parts = []
    for cache_key, _, flag in FIELD_SPECS:
        field = cache["fields"].get(cache_key)
        if not field:
            continue
        kind = "单选" if field.get("select_mode") else ("多选" if field.get("multi") else "非单选")
        parts.append("%s=%s (%s, %d 个选项)"
                     % (field.get("name"), field.get("field_id"), kind, len(field.get("options") or [])))
    if parts:
        print("[ONES] 字段: " + " | ".join(parts))


def print_rows(rows):
    if not rows:
        print("(没有需要处理的字段)")
        return
    key_width = max(disp_width(row["issue"]) for row in rows) + 2
    name_width = max(disp_width(row["field"]) for row in rows) + 2
    for row in rows:
        old = row["old"] if row["old"] is not None else "(未知)"
        mark = "[变更]" if row["changed"] else "[不变]"
        print("%s%s: %s -> %s   %s"
              % (pad(row["issue"], key_width), pad(row["field"], name_width), old, row["new"], mark))


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def run(args, logger):
    # 先做纯参数校验（不依赖环境变量），便于离线快速反馈
    wanted = collect_change_args(args)
    env = load_env()
    url_info = parse_ones_url(env["ones_bug"])
    targets = check_bug_args(args.bug, url_info)

    api = ApiClient(url_info["host"], env["ones_api"], env.get("ones_user_id"),
                    timeout=args.timeout, debug=args.debug, logger=logger)

    logger.info("启动: version=%s token=%s bugs=%s 参数=%s"
                % (SCRIPT_VERSION, mask(env["ones_api"]), ",".join(targets),
                   {k: v for k, v in wanted.items()}))

    cache_state = "hit"
    cache = None if args.force else load_cache()
    if cache is not None and not cache_matches(cache, url_info):
        print("[提示] 缓存与当前 ONES_BUG（host/team/project）不一致，重新构建")
        cache = None
    if cache is None:
        cache_state = "rebuilt"
        cache = build_cache(api, url_info)

    api.auth_header = cache["_meta"].get("auth_header")
    print_header(url_info, cache, cache_state)

    changes = prepare_changes(wanted, cache["fields"])
    wanted_ids = {change["field_id"] for change in changes}

    rows = []
    plan = []
    for key in targets:
        issue = resolve_issue_id(api, cache, key)
        current = get_current(api, cache["_meta"]["team_id"], issue["id"], wanted_ids)
        pending = []
        for change in changes:
            old = current.get(change["field_id"])
            changed = True if old is None else normalize(old) != normalize(change["label"])
            rows.append({
                "issue": key,
                "field": change["field_name"],
                "old": old,
                "new": change["label"],
                "changed": changed,
            })
            if changed:
                pending.append(change)
        plan.append({"key": key, "issue": issue, "changes": pending})

    print_rows(rows)
    print("-" * 72)

    if not args.yes:
        change_count = sum(len(item["changes"]) for item in plan)
        pending_issues = len([item for item in plan if item["changes"]])
        print("DRY-RUN: 待修改 %d 个 issue / %d 个字段；未做任何写操作。加 --yes 才会真正提交。"
              % (pending_issues, change_count))
        logger.info("DRY-RUN 结束: 待修改 issue %d 个 / 字段 %d 个" % (pending_issues, change_count))
        return 0

    success = 0
    failed = 0
    skipped = 0
    started = time.time()
    for item in plan:
        key = item["key"]
        if not item["changes"]:
            skipped += 1
            print("%sSKIP  无字段变化" % pad(key, 10))
            continue
        try:
            apply_update(api, cache["_meta"]["team_id"], item["issue"]["id"], item["changes"])
        except ApiError as exc:
            failed += 1
            print("%sFAIL  %s" % (pad(key, 10), exc.describe()))
            logger.error("issue %s 更新失败: %s" % (key, exc.describe()))
            continue
        success += 1
        print("%sPUT 200 SUCCESS  已更新 %d 个字段" % (pad(key, 10), len(item["changes"])))
        logger.info("issue %s 更新成功: %s"
                    % (key, ", ".join("%s=%s" % (c["field_name"], c["label"]) for c in item["changes"])))

    print("-" * 72)
    print("汇总: 成功 %d / 失败 %d / 跳过 %d    耗时 %.1fs"
          % (success, failed, skipped, time.time() - started))
    logger.info("结束: 成功 %d / 失败 %d / 跳过 %d" % (success, failed, skipped))
    return 0 if failed == 0 else 3


def main(argv=None):
    args = parse_args(argv)
    logger = Logger(os.path.join(SCRIPT_DIR, LOG_FILENAME))
    try:
        return run(args, logger)
    except UsageError as exc:
        print("[错误] %s" % exc)
        logger.error("参数/环境错误: %s" % exc)
        return 2
    except OptionError as exc:
        print("[错误] %s" % exc)
        if exc.options:
            print("[提示] 合法值如下（共 %d 个）:" % len(exc.options))
            for item in exc.options:
                print("  - %s" % item)
        logger.error("取值校验失败: %s" % exc)
        return 1
    except FieldError as exc:
        print("[错误] %s" % exc)
        if exc.available:
            print("[提示] team 内全部属性:")
            for item in exc.available:
                print("  - %s" % item)
        logger.error("属性定位失败: %s" % exc)
        return 1
    except ValidationError as exc:
        print("[错误] %s" % exc)
        logger.error("校验失败: %s" % exc)
        return 1
    except ApiError as exc:
        print("[错误] %s" % exc.describe())
        logger.error("API 错误: %s" % exc.describe())
        return 3
    except KeyboardInterrupt:
        print("[中断] 用户取消")
        return 3


if __name__ == "__main__":
    sys.exit(main())
