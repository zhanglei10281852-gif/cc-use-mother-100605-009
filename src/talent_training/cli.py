"""命令行操作接口：启动服务、执行端到端演示场景、校验审计链。

用法：
    python -m talent_training.cli serve --db data/talent.db
    python -m talent_training.cli demo  --db data/demo.db
    python -m talent_training.cli verify --db data/talent.db
"""
from __future__ import annotations

import argparse
import json
import time

from .service import Service


def seed_demo(svc: Service) -> dict:
    """构造一个覆盖核心矛盾的演示场景：
    - 高优先级紧缺工厂与普通工厂竞争名额
    - 同一学员出现在两个需求中（重复安排检查）
    - 小容量班级 + 紧张配额 -> 部分学员进入候补
    - 课程取消后名额回收、候补自动补上
    """
    svc.create_provider({"id": "prov_a", "name": "江南技师学院", "region": "苏南"})
    svc.create_provider({"id": "prov_b", "name": "江北实训基地", "region": "苏北"})

    svc.create_enterprise({"id": "ent_key", "name": "紧缺智造工厂", "region": "苏南",
                           "priority": 100})
    svc.create_enterprise({"id": "ent_std", "name": "普通机械厂", "region": "苏南",
                           "priority": 10})

    students = [
        {"id": "s1", "name": "张三", "home_region": "苏南", "enterprise_id": "ent_key",
         "skills": {"welding": 1}},
        {"id": "s2", "name": "李四", "home_region": "苏南", "enterprise_id": "ent_key",
         "skills": {"welding": 2}},
        {"id": "s3", "name": "王五", "home_region": "苏南", "enterprise_id": "ent_std",
         "skills": {"welding": 1}},
        {"id": "s4", "name": "赵六", "home_region": "苏北", "enterprise_id": "ent_std",
         "skills": {"welding": 0}},
    ]
    for s in students:
        svc.create_student(s)

    svc.create_class({"id": "cls_w1", "provider_id": "prov_a", "skill_code": "welding",
                      "level": 3, "region": "苏南", "capacity": 2, "year": 2026})
    # 年度配额只给 2 个，制造资源紧张
    svc.set_quota(2026, "苏南", "welding", 2)

    # s1 同时被两个需求提报 -> 第二轮应命中 R2 重复安排
    svc.create_need({"id": "need_key", "enterprise_id": "ent_key", "skill_code": "welding",
                     "target_level": 3, "allowed_regions": ["苏南"], "headcount": 2,
                     "year": 2026, "candidate_ids": ["s1", "s2"]})
    svc.create_need({"id": "need_std", "enterprise_id": "ent_std", "skill_code": "welding",
                     "target_level": 3, "allowed_regions": ["苏南", "苏北"],
                     "headcount": 2, "year": 2026,
                     "candidate_ids": ["s1", "s3", "s4"]})

    run = svc.run_allocation(actor="scheduler", note="年度首轮分配")
    return run


def cmd_demo(args) -> None:
    svc = Service(db_path=args.db, start_worker=False)
    try:
        run = seed_demo(svc)
        print("== 首轮分配 ==")
        print(json.dumps(run, ensure_ascii=False, indent=2))

        detail = svc.get_run(run["run_id"])
        print("\n== 决策与规则轨迹（前两条） ==")
        for dec in detail["decisions"][:2]:
            print(f"- {dec['student_id']}: {dec['result']} | {dec['reason']}")

        print("\n== 候补队列 ==")
        print(json.dumps(svc.list_waitlist(), ensure_ascii=False, indent=2))

        # 取消在籍班级，名额应回收并由候补补上（直接处理，不依赖后台线程）
        print("\n== 取消班级 cls_w1 ==")
        print(json.dumps(svc.cancel_class("cls_w1", reason="厂房检修"), ensure_ascii=False, indent=2))
        # 机构重开替代班，并追加年度配额
        svc.create_class({"id": "cls_w2", "provider_id": "prov_a", "skill_code": "welding",
                          "level": 3, "region": "苏南", "capacity": 3, "year": 2026})
        svc.set_quota(2026, "苏南", "welding", 4)  # 追加配额
        result = svc.process_waitlist_now()
        print("候补处理结果：", json.dumps(result, ensure_ascii=False))
        print(json.dumps(svc.list_waitlist(status="placed"),
                                        ensure_ascii=False, indent=2))

        print("\n== 配额台账 ==")
        print(json.dumps(svc.list_quota(2026), ensure_ascii=False, indent=2))

        print("\n== 审计链校验 ==")
        print(json.dumps(svc.verify_audit(), ensure_ascii=False, indent=2))
    finally:
        svc.shutdown()


def cmd_serve(args) -> None:
    from .api import create_server

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


def cmd_verify(args) -> None:
    svc = Service(db_path=args.db, start_worker=False)
    try:
        print(json.dumps(svc.verify_audit(), ensure_ascii=False, indent=2))
    finally:
        svc.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="省级数字人才调度系统")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="启动 HTTP 服务")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--db", default="data/talent.db")
    p_serve.add_argument("--poll", type=float, default=2.0)
    p_serve.set_defaults(func=cmd_serve)

    p_demo = sub.add_parser("demo", help="运行端到端演示场景")
    p_demo.add_argument("--db", default="data/demo.db")
    p_demo.set_defaults(func=cmd_demo)

    p_verify = sub.add_parser("verify", help="校验审计哈希链")
    p_verify.add_argument("--db", default="data/talent.db")
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
