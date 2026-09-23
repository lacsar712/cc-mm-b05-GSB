"""临时抬高报警线验收脚本（真实 HTTP/WS 服务）。

运行：PYTHONPATH=.pylibs:backend DATABASE_URL=sqlite:///./acceptance.db \
      python3 acceptance_test.py
覆盖：
1. 检查员抬线到 1.5 并设短失效；失效前报 1.2 判正常且不推送报警；履历留下 1.5。
2. 临时线生效期间推送规则跟新线走（达到 1.5 才推送）。
3. 失效后自动回到百分之一，再报 1.2 判报警并推送。
4. 每次抬高追加履历，旧临时线与失效时刻不被后续抬高覆盖。
5. 旁观账号只读履历，不能抬线。
"""
import asyncio
import os
import socket

os.environ.setdefault("DATABASE_URL", "sqlite:///./acceptance.db")
if os.path.exists("./acceptance.db"):
    os.remove("./acceptance.db")

import httpx  # noqa: E402
import uvicorn  # noqa: E402
import websockets  # noqa: E402

from app.main import app  # noqa: E402

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    line = f"[{status}] {name}"
    if not cond and detail:
        line += f" -- {detail}"
    print(line)
    if not cond:
        failures.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
WS = f"ws://127.0.0.1:{PORT}/ws/alerts"


async def recv_or_none(ws, timeout):
    try:
        return await asyncio.wait_for(ws.recv(), timeout=timeout)
    except asyncio.TimeoutError:
        return None


async def main():
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    server_task = asyncio.create_task(server.serve())
    await asyncio.sleep(1)  # 等 startup（建表、种子数据）

    try:
        async with httpx.AsyncClient(base_url=BASE, timeout=5) as http:
            def headers(token):
                return {"Authorization": f"Bearer {token}"}

            async def login(username, password):
                r = await http.post("/api/auth/login", json={"username": username, "password": password})
                check(f"登录 {username}", r.status_code == 200, r.text)
                return r.json()["access_token"]

            gasman = await login("gasman", "gas123456")
            viewer = await login("viewer", "view123456")

            # ---- 场景一：抬到 1.5，3 秒失效 ----
            print("\n== 抬线 1.5%，3 秒后失效 ==")
            r = await http.post(
                "/api/thresholds",
                headers=headers(gasman),
                json={"threshold_pct": 1.5, "expires_in_seconds": 3},
            )
            check("抬线返回 201", r.status_code == 201, f"{r.status_code} {r.text}")
            raised = r.json()
            check("返回临时线 1.5", raised["threshold_pct"] == 1.5)
            check("返回生效中", raised["active"] is True)

            cur = (await http.get("/api/thresholds/current", headers=headers(gasman))).json()
            check("当前线为生效临时线 1.5", cur["active"] and cur["threshold_pct"] == 1.5, str(cur))

            history = (await http.get("/api/thresholds/history", headers=headers(gasman))).json()
            check("履历留下 1.5", any(h["threshold_pct"] == 1.5 for h in history), str(history))
            first_expiry = next(h["expires_at"] for h in history if h["threshold_pct"] == 1.5)

            # ---- 失效前上报：1.2 正常，不推送 ----
            print("\n== 失效前上报 1.2 ==")
            async with websockets.connect(WS) as ws:
                r = await http.post(
                    "/api/readings",
                    headers=headers(gasman),
                    json={"site": "验收巷", "ch4_pct": 1.2},
                )
                check("上报返回 201", r.status_code == 201, r.text)
                body = r.json()
                check("1.2 判为正常", body["level"] == "正常", str(body))
                check("判定按临时线 1.5", body["alert_pct"] == 1.5, str(body))
                check("正常记录不推送报警", await recv_or_none(ws, 0.8) is None)

            # ---- 推送跟着新线走：1.6 >= 1.5 才推送（独立连接）----
            print("\n== 失效前上报 1.6（达到临时线）==")
            async with websockets.connect(WS) as ws:
                r = await http.post(
                    "/api/readings",
                    headers=headers(gasman),
                    json={"site": "验收巷", "ch4_pct": 1.6},
                )
                check("1.6 判为报警", r.json()["level"] == "报警", r.text)
                import json

                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                check("报警按临时线推送", msg["level"] == "报警" and msg["ch4_pct"] == 1.6, str(msg))

            # ---- 履历不被后续抬高覆盖 ----
            print("\n== 再次抬高：1.3%，6 秒失效 ==")
            r = await http.post(
                "/api/thresholds",
                headers=headers(gasman),
                json={"threshold_pct": 1.3, "expires_in_seconds": 6},
            )
            check("第二次抬线返回 201", r.status_code == 201, r.text)
            history = (await http.get("/api/thresholds/history", headers=headers(gasman))).json()
            check("履历有两条记录", len(history) == 2, str(history))
            old = next(h for h in history if h["threshold_pct"] == 1.5)
            new = next(h for h in history if h["threshold_pct"] == 1.3)
            check("旧临时线 1.5 仍在履历", old is not None)
            check("旧失效时刻未被覆盖", old["expires_at"] == first_expiry,
                  f"{old['expires_at']} != {first_expiry}")
            check("新线 1.3 为当前生效", new["active"] and old["active"] is False, str(history))

            # ---- 旁观账号只读 ----
            print("\n== 旁观账号权限 ==")
            r = await http.get("/api/thresholds/history", headers=headers(viewer))
            check("viewer 可只读履历", r.status_code == 200 and len(r.json()) == 2)
            r = await http.post(
                "/api/thresholds",
                headers=headers(viewer),
                json={"threshold_pct": 2.0, "expires_in_seconds": 60},
            )
            check("viewer 抬线被拒 403", r.status_code == 403, str(r.status_code))
            r = await http.post(
                "/api/readings",
                headers=headers(viewer),
                json={"site": "x", "ch4_pct": 0.1},
            )
            check("viewer 上报被拒 403", r.status_code == 403, str(r.status_code))

            # ---- 入参校验 ----
            print("\n== 入参校验 ==")
            r = await http.post(
                "/api/thresholds",
                headers=headers(gasman),
                json={"threshold_pct": 1.0, "expires_in_seconds": 60},
            )
            check("抬到 1.0 被拒（必须高于百分之一）", r.status_code == 422, str(r.status_code))
            r = await http.post(
                "/api/thresholds",
                headers=headers(gasman),
                json={"threshold_pct": 1.5, "expires_at": "2000-01-01T00:00:00+00:00"},
            )
            check("过去失效时刻被拒", r.status_code == 422, str(r.status_code))

            # ---- 等当前临时线失效，自动回到百分之一 ----
            print("\n== 等待临时线失效 ==")
            cur = {"active": True}
            for _ in range(16):
                await asyncio.sleep(0.5)
                cur = (await http.get("/api/thresholds/current", headers=headers(gasman))).json()
                if not cur["active"]:
                    break
            check("失效后当前线回到百分之一", not cur["active"] and cur["threshold_pct"] == 1.0, str(cur))
            history = (await http.get("/api/thresholds/history", headers=headers(gasman))).json()
            check("履历记录保留且全部标记已失效",
                  all(h["active"] is False for h in history) and len(history) == 2, str(history))

            # ---- 失效后上报 1.2：报警并按百分之一推送 ----
            print("\n== 失效后上报 1.2 ==")
            async with websockets.connect(WS) as ws:
                r = await http.post(
                    "/api/readings",
                    headers=headers(gasman),
                    json={"site": "验收巷", "ch4_pct": 1.2},
                )
                body = r.json()
                check("失效后 1.2 判为报警", body["level"] == "报警" and body["alert_pct"] == 1.0, str(body))
                import json

                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                check("失效后报警按百分之一推送",
                      msg["level"] == "报警" and msg["ch4_pct"] == 1.2, str(msg))
    finally:
        server.should_exit = True
        await server_task

    print("\n" + ("全部通过 ✅" if not failures else f"失败 {len(failures)} 项: {failures} ❌"))
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
