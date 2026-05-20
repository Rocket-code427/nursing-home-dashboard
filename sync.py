#!/usr/bin/env python3
"""
养老院项目 - 飞书 Base 数据同步脚本
从飞书 Base 读取数据，生成 data.json 供看板使用

用法:
    python sync.py

环境变量:
    FEISHU_APP_ID      - 飞书应用 ID
    FEISHU_APP_SECRET  - 飞书应用 Secret
    FEISHU_BASE_TOKEN  - Base 的 app_token
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

# 配置
BASE_URL = "https://open.feishu.cn/open-apis"
APP_ID = "cli_a9675dd60ef89bcd"
APP_SECRET = "NmDOGCOoo6bNtR9eCinr7f0llvBpXWMF"
BASE_TOKEN = "QL33bL9Fda3FpDs8JOicw9TVnJe"

# 表名配置（可根据实际情况修改）
TABLE_DEVICES = "全部设备排期"
TABLE_MEETINGS = "会议纪要"
TABLE_TASKS = "节点任务"

# 节点名称映射（根据飞书Base实际字段）
NODE_NAMES = ["需求确认", "协议提供", "开发阶段", "联调测试", "正式上线"]


def get_tenant_access_token():
    """获取 tenant_access_token"""
    url = f"{BASE_URL}/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={
        "app_id": APP_ID,
        "app_secret": APP_SECRET
    }, timeout=30)
    data = resp.json()
    if data.get("code") != 0:
        print(f"获取 token 失败: {data}")
        sys.exit(1)
    return data["tenant_access_token"]


def list_tables(token):
    """列出 Base 中所有表格"""
    url = f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=30)
    data = resp.json()
    if data.get("code") != 0:
        print(f"列出表格失败: {data}")
        sys.exit(1)
    return {t["name"]: t["table_id"] for t in data["data"]["items"]}


def list_records(token, table_id, view_id=None):
    """读取表格所有记录"""
    all_records = []
    page_token = None
    headers = {"Authorization": f"Bearer {token}"}

    while True:
        url = f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records"
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        if view_id:
            params["view_id"] = view_id

        resp = requests.get(url, headers=headers, params=params, timeout=30)
        data = resp.json()
        if data.get("code") != 0:
            print(f"读取记录失败: {data}")
            break

        items = data["data"]["items"]
        all_records.extend(items)

        if not data["data"].get("has_more"):
            break
        page_token = data["data"].get("page_token")

    return all_records


def format_date(date_raw):
    """格式化日期字段：支持时间戳/字符串/字典"""
    if not date_raw:
        return ""
    if isinstance(date_raw, (int, float)):
        if date_raw > 1000000000000:  # 毫秒时间戳
            return datetime.fromtimestamp(date_raw / 1000).strftime("%m/%d")
        elif date_raw > 1000000000:  # 秒时间戳
            return datetime.fromtimestamp(date_raw).strftime("%m/%d")
    elif isinstance(date_raw, str):
        # 尝试解析常见格式
        for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%m-%d", "%m/%d"]:
            try:
                dt = datetime.strptime(date_raw, fmt)
                return dt.strftime("%m/%d")
            except ValueError:
                continue
        return date_raw  # 返回原始字符串
    elif isinstance(date_raw, dict):
        # 飞书可能返回 {text: "..."} 或 {value: "..."}
        return format_date(date_raw.get("text") or date_raw.get("value"))
    return str(date_raw)


def parse_devices(records):
    """解析设备排期表 - 根据实际飞书字段映射"""
    devices = []
    for r in records:
        fields = r.get("fields", {})
        name = fields.get("设备品类", "")
        if not name:
            continue

        # 节点1-4: 以复选框为准，不受"当前进度"文字覆盖
        n1 = "completed" if fields.get("需求确认完成") is True else "pending"
        n2 = "completed" if fields.get("协议提供完成") is True else "pending"
        
        # 节点3: 开发阶段（APP + 主机）
        app_status = fields.get("APP开发", "")
        host_status = fields.get("主机开发", "")
        if app_status in ["已完成", "完成", "Done", "已接入"] and host_status in ["已完成", "完成", "Done", "已接入", "需开发"]:
            n3 = "completed"
        elif app_status or host_status:
            n3 = "in_progress"
        else:
            n3 = "pending"
        
        n4 = "completed" if fields.get("开发联调完成") is True else "pending"
        
        # 节点5: 正式上线（最终上线完成复选框）
        n5 = "completed" if fields.get("最终上线完成") is True else "pending"
        
        # 仅当「当前进度」是明确的完成状态时微调
        current = fields.get("当前进度", "")
        if current in ["已完成", "上线", "已上线", "Done"]:
            n1 = n2 = n3 = n4 = n5 = "completed"
        elif current == "提测" and n5 != "completed":
            n5 = "in_progress"
        elif current == "开发中" and n3 != "completed":
            n3 = "in_progress"
        # "需求确认"/"协议提供"等进度文字不再覆盖复选框状态
        elif current == "":
            # 当前进度为空时，根据已完成的复选框推断
            completed_count = sum([n1 == "completed", n2 == "completed", n3 == "completed", n4 == "completed"])
            if completed_count == 4 and final_date:
                n5 = "in_progress"  # 前4节点都完了，有上线日期，等待上线
            elif completed_count == 4 and not final_date:
                n5 = "pending"  # 前4节点都完了，但无上线日期
            elif completed_count >= 2:
                pass  # 保持复选框判断的状态
            else:
                # 如果大部分都没完成但最终日期有值，可能只是刚建记录
                pass

        nodes = [
            {"status": n1, "name": "需求确认", "date": format_date(fields.get("需求确认"))},
            {"status": n2, "name": "协议提供", "date": format_date(fields.get("协议提供"))},
            {"status": n3, "name": "开发阶段", "date": format_date(fields.get("开发联调"))},
            {"status": n4, "name": "联调测试", "date": format_date(fields.get("提测"))},
            {"status": n5, "name": "正式上线", "date": format_date(fields.get("最终上线节点"))},
        ]
        
        devices.append({
            "name": name,
            "model": fields.get("型号", ""),
            "mid": fields.get("MID（细分码）", ""),
            "owner": fields.get("负责人", ""),
            "nextAction": fields.get("下一步行动", ""),
            "nodes": nodes
        })
    
    return devices


def parse_risks(records):
    """从设备记录中解析风险项（下一步行动不为空 = 风险/待跟进）"""
    risks = []
    for r in records:
        fields = r.get("fields", {})
        name = fields.get("设备品类", "")
        action = fields.get("下一步行动", "")
        current = fields.get("当前进度", "")
        
        if action:  # 有下一步行动 = 需要跟进
            # 判断风险等级
            level = "P0" if current in ["需求确认", "", "待启动"] else "P1"
            risks.append({
                "id": len(risks) + 1,
                "title": f"{name} - {action[:30]}",
                "description": action,
                "device": name,
                "level": level
            })
    
    return risks


def parse_meetings(records):
    """解析会议纪要表"""
    meetings = []
    for r in records:
        fields = r.get("fields", {})
        no = fields.get("NO.", fields.get("编号", ""))
        title = fields.get("会议主题", fields.get("主题", fields.get("标题", "未命名会议")))
        date_raw = fields.get("会议日期", fields.get("日期", ""))
        participants = fields.get("参与人", fields.get("参会人", ""))
        summary = fields.get("会议纪要", fields.get("摘要", fields.get("纪要", "")))
        actions_raw = fields.get("待办事项", fields.get("待办", ""))

        # 日期格式转换：时间戳 → YYYY-MM-DD
        date = ""
        if isinstance(date_raw, (int, float)) and date_raw > 1000000000000:
            # 毫秒时间戳
            date = datetime.fromtimestamp(date_raw / 1000).strftime("%Y-%m-%d")
        elif isinstance(date_raw, str):
            date = date_raw

        actions = []
        if isinstance(actions_raw, str):
            actions = [a.strip() for a in actions_raw.split("\n") if a.strip()]
        elif isinstance(actions_raw, list):
            actions = [str(a) for a in actions_raw]

        meetings.append({
            "id": r.get("record_id", ""),
            "no": str(no),
            "title": title,
            "date": date,
            "participants": participants,
            "summary": summary,
            "actions": actions
        })

    # 按编号倒序，最新的在前
    meetings.sort(key=lambda m: m["no"], reverse=True)
    return meetings


def parse_risks(records, devices=None):
    """从设备记录中解析风险项（下一步行动不为空 = 风险/待跟进）"""
    risks = []
    for r in records:
        fields = r.get("fields", {})
        name = fields.get("设备品类", "")
        action = fields.get("下一步行动", "")
        current = fields.get("当前进度", "")
        
        if action:  # 有下一步行动 = 需要跟进
            # 判断风险等级：进度为空或在早期阶段 = P0，否则 P1
            level = "P0" if current in ["需求确认", "", "待启动", "待确认"] else "P1"
            risks.append({
                "id": len(risks) + 1,
                "title": f"{name} - {action[:40]}",
                "description": action,
                "device": name,
                "level": level
            })
    
    return risks


def build_dashboard_data(token, tables):
    """构建看板数据"""
    # 读取设备表
    device_records = list_records(token, tables[TABLE_DEVICES]) if TABLE_DEVICES in tables else []
    devices = parse_devices(device_records)

    # 读取会议表
    meeting_records = list_records(token, tables[TABLE_MEETINGS]) if TABLE_MEETINGS in tables else []
    meetings = parse_meetings(meeting_records)

    # 读取风险（从设备表和任务表综合）
    risks = parse_risks(device_records, devices)

    # 构建最终数据
    dashboard_data = {
        "updateTime": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "devices": devices,
        "risks": risks,
        "meetings": meetings
    }

    return dashboard_data


def main():
    print("🚀 开始同步养老院项目数据...")

    # 检查配置
    if not APP_ID or not APP_SECRET:
        print("❌ 请设置环境变量 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        print("   或使用: export FEISHU_APP_ID=xxx FEISHU_APP_SECRET=xxx")
        sys.exit(1)

    if not BASE_TOKEN:
        print("❌ 请设置环境变量 FEISHU_BASE_TOKEN（Base 的 app_token）")
        sys.exit(1)

    # 获取 token
    print("🔑 获取飞书访问令牌...")
    token = get_tenant_access_token()
    print("✅ Token 获取成功")

    # 列出表格
    print("📋 读取 Base 表格列表...")
    tables = list_tables(token)
    print(f"✅ 发现 {len(tables)} 个表格: {', '.join(tables.keys())}")

    # 检查必需的表是否存在
    for tname in [TABLE_DEVICES, TABLE_MEETINGS]:
        if tname not in tables:
            print(f"⚠️ 警告: 未找到表格「{tname}」，请检查表名是否正确")

    # 构建数据
    print("📊 构建看板数据...")
    data = build_dashboard_data(token, tables)

    # 写入 data.json
    output_path = Path(__file__).parent / "data.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"✅ 数据同步完成！")
    print(f"   设备数: {len(data['devices'])}")
    print(f"   风险数: {len(data['risks'])}")
    print(f"   会议数: {len(data['meetings'])}")
    print(f"   输出文件: {output_path.absolute()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
