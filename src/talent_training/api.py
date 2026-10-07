"""HTTP API（标准库实现，无第三方依赖）。

路由概览：
  实体登记   POST /api/providers | /enterprises | /students | /classes | /needs
             GET  同路径（列表）；GET .../{id} 详情
  批量导入   POST /api/imports
  配额       POST /api/quotas   GET /api/quotota?year=   GET /api/ledger
             POST /api/quotas/carry-over
  分配       POST /api/allocations/run   GET /api/allocations   GET /api/allocations/{id}
  生命周期   POST /api/classes/{id}/cancel
             POST /api/providers/{id}/suspend | /resume
             POST /api/enrollments/{id}/attendance | /complete | /cancel | /transfer
  候补       GET  /api/waitlist?status=   POST /api/waitlist/process
  审计       GET  /api/audit?entity_type=&entity_id=   POST /api/audit/verify

所有写操作接受可选 X-Actor 头标识操作人。
"""
from __future__ import annotations

import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .service import Service


def _build_routes(svc: Service):
    """返回 [(方法, 编译正则, 处理函数)] 形式的路由表。"""
    def set_quota(h, p):
        b = h.body()
        return svc.set_quota(int(b["year"]), b["region"], b["skill_code"],
                             int(b["total"]), h.actor())

    return [
        ("POST", r"^/api/providers$", lambda h, p: svc.create_provider(h.body(), h.actor())),
        ("GET", r"^/api/providers$", lambda h, p: svc.list_providers()),
        ("POST", r"^/api/enterprises$", lambda h, p: svc.create_enterprise(h.body(), h.actor())),
        ("GET", r"^/api/enterprises$", lambda h, p: svc.list_enterprises()),
        ("POST", r"^/api/students$", lambda h, p: svc.create_student(h.body(), h.actor())),
        ("GET", r"^/api/students$", lambda h, p: svc.list_students()),
        ("GET", r"^/api/students/(?P<id>[^/]+)$", lambda h, p: h.require(svc.get_student(p["id"]))),
        ("POST", r"^/api/classes$", lambda h, p: svc.create_class(h.body(), h.actor())),
        ("GET", r"^/api/classes$", lambda h, p: svc.list_classes(h.query_int("year"))),
        ("GET", r"^/api/classes/(?P<id>[^/]+)$", lambda h, p: h.require(svc.get_class(p["id"]))),
        ("POST", r"^/api/classes/(?P<id>[^/]+)/cancel$",
         lambda h, p: svc.cancel_class(p["id"], h.actor(), h.body().get("reason", "课程临时取消"))),
        ("POST", r"^/api/providers/(?P<id>[^/]+)/suspend$",
         lambda h, p: svc.suspend_provider(p["id"], h.actor(), h.body().get("reason", "机构停课"))),
        ("POST", r"^/api/providers/(?P<id>[^/]+)/resume$",
         lambda h, p: svc.resume_provider(p["id"], h.actor())),
        ("POST", r"^/api/needs$", lambda h, p: svc.create_need(h.body(), h.actor())),
        ("GET", r"^/api/needs$", lambda h, p: svc.list_needs(h.query("status"))),
        ("GET", r"^/api/needs/(?P<id>[^/]+)$", lambda h, p: h.require(svc.get_need(p["id"]))),
        ("POST", r"^/api/imports$", lambda h, p: svc.import_batch(h.body(), h.actor())),
        ("GET", r"^/api/imports$", lambda h, p: svc.list_imports()),
        ("POST", r"^/api/quotas$",
         lambda h, p: set_quota(h, p)),
        ("GET", r"^/api/quotas$", lambda h, p: svc.list_quota(h.query_int("year"))),
        ("GET", r"^/api/ledger$", lambda h, p: svc.list_ledger(h.query_int("year"))),
        ("POST", r"^/api/quotas/carry-over$",
         lambda h, p: svc.carry_over(int(h.body()["from_year"]), int(h.body()["to_year"]),
                                     h.actor())),
        ("POST", r"^/api/allocations/run$",
         lambda h, p: svc.run_allocation(h.actor(), h.body().get("note", ""),
                                         h.body().get("need_ids"))),
        ("GET", r"^/api/allocations$", lambda h, p: svc.list_runs()),
        ("GET", r"^/api/allocations/(?P<id>[^/]+)$",
         lambda h, p: h.require(svc.get_run(p["id"]))),
        ("POST", r"^/api/enrollments/(?P<id>[^/]+)/attendance$",
         lambda h, p: svc.mark_attendance(p["id"], bool(h.body()["attended"]), h.actor())),
        ("POST", r"^/api/enrollments/(?P<id>[^/]+)/complete$",
         lambda h, p: svc.complete(p["id"], h.actor())),
        ("POST", r"^/api/enrollments/(?P<id>[^/]+)/cancel$",
         lambda h, p: svc.cancel_enrollment(p["id"], h.actor(),
                                            h.body().get("reason", "学员退课"))),
        ("POST", r"^/api/enrollments/(?P<id>[^/]+)/transfer$",
         lambda h, p: svc.transfer_student(p["id"], h.body()["target_class_id"], h.actor(),
                                           h.body().get("reason", "学员转班"))),
        ("GET", r"^/api/enrollments$",
         lambda h, p: svc.list_enrollments(h.query("class_id"), h.query("student_id"))),
        ("GET", r"^/api/waitlist$", lambda h, p: svc.list_waitlist(h.query("status"))),
        ("POST", r"^/api/waitlist/process$", lambda h, p: svc.process_waitlist_now()),
        ("GET", r"^/api/audit$",
         lambda h, p: svc.list_audit(int(h.query("limit") or 100),
                                     h.query("entity_type"), h.query("entity_id"))),
        ("POST", r"^/api/audit/verify$", lambda h, p: svc.verify_audit()),
        ("GET", r"^/api/health$", lambda h, p: {"status": "ok"}),
    ]


class ApiHandler(BaseHTTPRequestHandler):
    routes: list = []
    server_version = "TalentScheduler/1.0"

    def log_message(self, fmt, *args):  # 安静一点
        pass

    def actor(self) -> str:
        return self.headers.get("X-Actor", "admin")

    def body(self) -> dict:
        cached = getattr(self, "_body_cache", None)
        if cached is not None:
            return cached
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            self._body_cache = {}
            return self._body_cache
        raw = self.rfile.read(length)
        self._body_cache = json.loads(raw.decode("utf-8")) if raw else {}
        return self._body_cache

    def query(self, key: str) -> str | None:
        qs = parse_qs(urlparse(self.path).query)
        vals = qs.get(key)
        return vals[0] if vals else None

    def query_int(self, key: str) -> int | None:
        v = self.query(key)
        return int(v) if v is not None else None

    def require(self, value):
        if value is None:
            raise NotFound("资源不存在")
        return value

    def _send(self, code: int, payload) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            for m, pattern, fn in self.routes:
                if m != method:
                    continue
                match = re.match(pattern + "$", path)
                if match:
                    result = fn(self, match.groupdict())
                    self._send(200, {"ok": True, "data": result})
                    return
            self._send(404, {"ok": False, "error": f"无此路由: {method} {path}"})
        except NotFound as exc:
            self._send(404, {"ok": False, "error": str(exc)})
        except ValueError as exc:
            self._send(400, {"ok": False, "error": str(exc)})
        except sqlite3.IntegrityError as exc:
            self._send(400, {"ok": False, "error": f"数据冲突：{exc}"})
        except json.JSONDecodeError as exc:
            self._send(400, {"ok": False, "error": f"请求体不是合法 JSON: {exc}"})
        except Exception as exc:  # noqa: BLE001 - 统一兜底，避免线程崩溃
            self._send(500, {"ok": False, "error": f"服务器内部错误: {exc!r}"})


class NotFound(Exception):
    pass


def create_server(host: str = "127.0.0.1", port: int = 8080,
                  db_path: str = "data/talent.db", poll_interval: float = 2.0):
    svc = Service(db_path=db_path, poll_interval=poll_interval)
    ApiHandler.routes = _build_routes(svc)
    httpd = ThreadingHTTPServer((host, port), ApiHandler)
    httpd.service = svc  # 挂到 server 上方便关闭
    return httpd, svc


def main():
    import argparse

    parser = argparse.ArgumentParser(description="省级数字人才调度系统")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="data/talent.db")
    parser.add_argument("--poll", type=float, default=2.0, help="候补轮询间隔秒数")
    args = parser.parse_args()

    httpd, svc = create_server(args.host, args.port, args.db, args.poll)
    print(f"服务已启动: http://{args.host}:{args.port}  数据库: {args.db}")
    print(f"启动恢复：{svc.recovered_waitlist} 条中断候补已复位继续处理")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        svc.shutdown()
        print("服务已停止")


if __name__ == "__main__":
    main()
