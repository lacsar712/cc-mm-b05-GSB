"""临时抬高报警线 — 端到端验收。

用 sqlite 临时库起整个 FastAPI 应用，走真实 HTTP/WebSocket 流程：
  抬 1.5 短线 -> 报 1.2 判正常且不推送 -> 履历留 1.5 -> 失效后报 1.2 判报警并推送
  另覆盖：旁观账号只读、再次抬高不覆盖旧履历、非法抬线被拒。

运行：cd backend && python3 tests/test_acceptance.py
"""

import os
import sys
import tempfile
import time

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mktemp(suffix='.db')}"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.rules import classify  # noqa: E402

PASS = []


def check(name, cond):
    assert cond, f"FAIL: {name}"
    PASS.append(name)
    print(f"  ok - {name}")


def login(client, username, password):
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def main():
    print("[1] classify 规则")
    check("默认线 1.0：0.99 正常", classify(0.99)[0] == "正常")
    check("默认线 1.0：1.0 报警", classify(1.0)[0] == "报警")
    check("临时线 1.5：1.2 正常", classify(1.2, 1.5)[0] == "正常")
    check("临时线 1.5：1.5 报警", classify(1.5, 1.5)[0] == "报警")

    with TestClient(app) as client:
        gasman = login(client, "gasman", "gas123456")
        viewer = login(client, "viewer", "view123456")

        print("[2] 初始状态（种子数据按 1% 判定）")
        readings = client.get("/api/readings", headers=gasman).json()
        by_site = {r["site"]: r["level"] for r in readings}
        check("回风巷 1.4 报警", by_site["回风巷"] == "报警")
        check("东翼-12 0.35 正常", by_site["东翼-12"] == "正常")
        st = client.get("/api/thresholds", headers=gasman).json()
        check("初始当前线 1.0", st["current"] == 1.0)
        check("初始无生效临时线", st["active_raise"] is None)
        check("初始履历为空", st["history"] == [])

        print("[3] 旁观账号只读")
        check("viewer 可读履历", client.get("/api/thresholds", headers=viewer).status_code == 200)
        check("viewer 抬线被拒", client.post("/api/thresholds", headers=viewer,
              json={"threshold": 1.5, "valid_seconds": 60}).status_code == 403)
        check("viewer 上报被拒", client.post("/api/readings", headers=viewer,
              json={"site": "x", "ch4_pct": 2.0}).status_code == 403)
        check("未登录不可读履历", client.get("/api/thresholds").status_code == 401)

        print("[4] 非法抬线被拒")
        check("临时线必须高于 1.0", client.post("/api/thresholds", headers=gasman,
              json={"threshold": 1.0, "valid_seconds": 60}).status_code == 422)
        check("有效期必须为正", client.post("/api/thresholds", headers=gasman,
              json={"threshold": 1.5, "valid_seconds": 0}).status_code == 422)

        print("[5] 抬到 1.5，2 秒后失效；临时线生效期间推送规则跟着新线走")
        with client.websocket_connect("/ws/alerts") as ws:
            res = client.post("/api/thresholds", headers=gasman,
                              json={"threshold": 1.5, "valid_seconds": 2})
            check("抬线成功", res.status_code == 201)
            raised = res.json()
            check("履历返回 1.5", raised["threshold"] == 1.5)
            check("履历带失效时刻", raised["expires_at"] > raised["created_at"])

            st = client.get("/api/thresholds", headers=viewer).json()
            check("当前线变为 1.5", st["current"] == 1.5)
            check("生效临时线为 1.5", st["active_raise"]["threshold"] == 1.5)

            res = client.post("/api/readings", headers=gasman,
                              json={"site": "运输巷", "ch4_pct": 1.2})
            check("临时线期间 1.2 判正常", res.json()["level"] == "正常")
            check("响应告知按 1.5 判定", res.json()["threshold"] == 1.5)

            res = client.post("/api/readings", headers=gasman,
                              json={"site": "采面", "ch4_pct": 1.6})
            check("临时线期间 1.6 判报警", res.json()["level"] == "报警")

            pushed = ws.receive_json()
            check("1.2 未推送、1.6 报警被推送（队列首条即 1.6）",
                  pushed["ch4_pct"] == 1.6 and pushed["level"] == "报警")

            print("[6] 等待失效，自动回到 1%")
            time.sleep(2.3)
            st = client.get("/api/thresholds", headers=viewer).json()
            check("失效后当前线回到 1.0", st["current"] == 1.0)
            check("失效后无生效临时线", st["active_raise"] is None)
            check("履历仍留 1.5 那条", len(st["history"]) == 1 and st["history"][0]["threshold"] == 1.5)

            res = client.post("/api/readings", headers=gasman,
                              json={"site": "运输巷", "ch4_pct": 1.2})
            check("失效后 1.2 判报警", res.json()["level"] == "报警")
            check("失效后按 1.0 判定", res.json()["threshold"] == 1.0)
            pushed = ws.receive_json()
            check("失效后 1.2 报警被推送", pushed["ch4_pct"] == 1.2 and pushed["level"] == "报警")

        print("[7] 再次抬高不覆盖旧履历")
        client.post("/api/thresholds", headers=gasman, json={"threshold": 1.8, "valid_seconds": 600})
        st = client.get("/api/thresholds", headers=viewer).json()
        thresholds = sorted(h["threshold"] for h in st["history"])
        check("履历两条都在（1.5 与 1.8）", thresholds == [1.5, 1.8])
        check("旧履历失效时刻未被改写",
              st["history"][1]["expires_at"] == raised["expires_at"])
        check("当前线取最新有效抬线 1.8", st["current"] == 1.8)

    print(f"\n全部通过：{len(PASS)} 项")


if __name__ == "__main__":
    main()
