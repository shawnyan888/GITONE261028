# -*- coding: utf-8 -*-
"""update_bug.py 离线自测脚本（辅助工具，不属于交付物）。

用 FakeApi 驱动 update_bug.py 的完整流程，不访问任何网络，缓存与日志写到系统临时目录。
运行: python selftest_update_bug.py
"""
import importlib.util
import json
import os
import tempfile

TARGET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "update_bug.py")
spec = importlib.util.spec_from_file_location("ub", TARGET)
ub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ub)

tmp = tempfile.mkdtemp(prefix="ones_test_")
ub.SCRIPT_DIR = tmp
PID = "6zAPyNY5xxxxxxxxxxxxxxxx"

FIELDS = [
    {"name": "缺陷类型", "id": "f1", "typeEnum": 1, "aliases": ["缺陷分类"],
     "options": [{"id": "o1", "value": "功能问题"}, {"id": "o2", "value": "安全问题"}]},
    {"name": "严重程度", "id": "f2", "typeEnum": 1,
     "options": [{"id": "o3", "value": "一般"}, {"id": "o4", "value": "致命"}]},
    {"name": "ling-build-version", "id": "f3", "typeEnum": 1,
     "options": [{"id": "o5", "value": "fusion2026.1.100.01"},
                 {"id": "o6", "value": "fusion2026.1.99.02"}]},
]
ID2LABEL = {f["id"]: {o["id"]: o["value"] for o in f["options"]} for f in FIELDS}


class FakeApi:
    instances = []
    # 类级状态：跨多次 main() 调用保持，用于验证幂等
    state = {"FUCO-7": {"f1": "功能问题", "f2": "一般", "f3": "fusion2026.1.99.02"}}

    def __init__(self, host, token, user_id=None, timeout=30, debug=False, logger=None):
        self.host = host
        self.token = token
        self.debug = debug
        self.logger = logger
        self.auth_header = None
        self.puts = []
        FakeApi.instances.append(self)

    def probe_auth(self, team_id):
        if self.token.startswith("bad"):
            raise ub.ApiError(None, "AUTH_FAILED", "Access token is invalid",
                              {"errorCode": "Unauthorized"})
        return ub.AUTH_TOKEN

    def probe_issue_key(self, team_id, issue_key):
        return True

    def request(self, method, path, params=None, body=None, auth_header=None):
        if method == "GET" and path == "/project/issueFields":
            return {"result": "SUCCESS", "data": {"list": FIELDS, "pageInfo": {"hasNextPage": False}}}
        if method == "GET" and path == "/project/projects":
            return {"result": "SUCCESS",
                    "data": {"list": [{"id": PID, "name": "FUCO"}],
                             "pageInfo": {"hasNextPage": False}}}
        if method == "GET" and path.startswith("/project/issues/"):
            key = path.rsplit("/", 1)[-1]
            if key.startswith("id-"):
                key = key[3:]
            cur = self.state.setdefault(key, {})
            return {"result": "SUCCESS", "data": {
                "id": "id-" + key, "number": int(key.split("-")[1]), "project": {"id": PID},
                "fieldValues": [{"fieldID": k, "type": 1, "value": v} for k, v in cur.items()]}}
        if method == "PUT" and path.startswith("/project/issues/"):
            key = path.rsplit("/", 1)[-1]
            if key.startswith("id-"):
                key = key[3:]
            self.puts.append((key, body["fieldValues"]))
            for item in body["fieldValues"]:
                self.state[key][item["fieldID"]] = ID2LABEL[item["fieldID"]].get(
                    item["value"], item["value"])
            return {"result": "SUCCESS", "data": {"id": "id-" + key}}
        raise AssertionError("unexpected %s %s" % (method, path))


def line(title):
    print("\n===== %s =====" % title)


def main():
    ub.ApiClient = FakeApi
    os.environ["ONES_BUG"] = ("https://ones.cn/project/#/team/KKv1z9x9/project/FUCO/component/"
                              "6zAPyNY5/nav/x/view/y/issue/FUCO-7")
    os.environ["ONES_API"] = "good-token"
    args = ["--bug", "FUCO-7", "--leixing", "安全问题", "--chengdu", "致命",
            "--lingling", "fusion2026.1.100.01"]
    results = []

    line("RUN1 首次运行 DRY-RUN")
    results.append(("RUN1", ub.main(args), 0))

    line("RUN2 --yes 实际提交")
    results.append(("RUN2", ub.main(args + ["--yes"]), 0))
    print("PUT payload:", json.dumps(FakeApi.instances[-1].puts, ensure_ascii=False))

    line("RUN3 --yes 幂等（应全部不变、无 PUT）")
    results.append(("RUN3", ub.main(args + ["--yes"]), 0))
    print("puts count:", len(FakeApi.instances[-1].puts))

    line("RUN4 非法取值")
    results.append(("RUN4", ub.main(["--bug", "FUCO-7", "--leixing", "不存在的类型"]), 1))

    line("RUN5 坏 token + --force")
    os.environ["ONES_API"] = "bad-token"
    results.append(("RUN5", ub.main(args + ["--force"]), 3))

    line("RUN6 缓存损坏自动重建")
    os.environ["ONES_API"] = "good-token"
    with open(os.path.join(tmp, ub.CACHE_FILENAME), "w", encoding="utf-8") as handle:
        handle.write("{broken")
    results.append(("RUN6", ub.main(args), 0))

    line("RESULT")
    ok = True
    for name, got, want in results:
        flag = "OK" if got == want else "FAIL"
        if got != want:
            ok = False
        print("%s %s exit=%s expect=%s" % (flag, name, got, want))
    with open(os.path.join(tmp, ub.CACHE_FILENAME), encoding="utf-8") as handle:
        cache = json.load(handle)
    print("cache keys:", sorted(cache.keys()))
    print("meta keys:", sorted(cache["_meta"].keys()))
    print("issues:", cache["issues"])
    print("log file:", os.path.isfile(os.path.join(tmp, ub.LOG_FILENAME)))
    print("tmpdir:", tmp)
    print("ALL OK" if ok else "SOME FAILED")


if __name__ == "__main__":
    main()
