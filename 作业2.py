#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


EASTMONEY_HOSTS = (
    "https://push2.eastmoney.com",
    "https://82.push2.eastmoney.com",
)
EASTMONEY_PATH = "/api/qt/clist/get"

EASTMONEY_FIELDS = (
    "f2,f3,f4,f5,f6,f7,f8,f9,f10,"
    "f12,f13,f14,f15,f16,f17,f18,f20,f21,f23"
)

EASTMONEY_BASE_PARAMS = {
    "po": 1,        
    "np": 1,
    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    "fltt": 2,
    "invt": 2,
    "fid": "f3",     
   
    "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
    "fields": EASTMONEY_FIELDS,
}

SINA_LIST_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
SINA_COUNT_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeStockCount"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/center/gridlist.html",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks (
    code             TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    market           TEXT,
    source           TEXT,
    latest_price     REAL,
    change_percent   REAL,
    change_amount    REAL,
    volume           REAL,   -- 成交量，统一换算为“股”
    turnover         REAL,   -- 成交额，单位：元
    amplitude        REAL,   -- 振幅，单位：%
    turnover_rate    REAL,   -- 换手率，单位：%
    pe_dynamic       REAL,   -- 动态市盈率
    pb               REAL,   -- 市净率
    volume_ratio     REAL,   -- 量比
    high             REAL,
    low              REAL,
    open             REAL,
    prev_close       REAL,
    total_market_cap REAL,   -- 总市值，单位：元
    float_market_cap REAL,   -- 流通市值，单位：元
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_stocks_change
    ON stocks (change_percent);
"""

STOCK_COLUMNS = (
    "code", "name", "market", "source",
    "latest_price", "change_percent", "change_amount",
    "volume", "turnover", "amplitude", "turnover_rate",
    "pe_dynamic", "pb", "volume_ratio",
    "high", "low", "open", "prev_close",
    "total_market_cap", "float_market_cap",
)


def build_session() -> requests.Session:
    """创建带自动重试机制的 requests 会话。"""
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(HEADERS)
    return session


def connect_db(db_path: Path) -> sqlite3.Connection:
    """连接（或创建）SQLite 数据库并初始化数据表。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def to_float(value: Any) -> Optional[float]:
    """把接口返回的数值安全转换为 float；停牌等情况返回 None。"""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "--", "null", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def market_name(code: str, market_flag: Optional[int] = None) -> str:
    """根据股票代码或东方财富市场标志判断所属市场。"""
    if code.startswith(("4", "8", "9")):
        return "北交所"
    if market_flag == 1:
        return "沪市"
    if market_flag == 0:
        return "深市"
    return "其他"


def normalize_eastmoney(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把东方财富接口的一条原始 JSON 数据转换为统一字段。"""
    code = str(item.get("f12") or "").strip()
    name = str(item.get("f14") or "").strip()
    if not code or not name:
        return None

    market_flag = None
    if item.get("f13") is not None:
        try:
            market_flag = int(item["f13"])
        except (TypeError, ValueError):
            market_flag = None

    volume_lots = to_float(item.get("f5"))
    return {
        "code": code,
        "name": name,
        "market": market_name(code, market_flag),
        "source": "eastmoney",
        "latest_price": to_float(item.get("f2")),
        "change_percent": to_float(item.get("f3")),
        "change_amount": to_float(item.get("f4")),
  
        "volume": volume_lots * 100 if volume_lots is not None else None,
        "turnover": to_float(item.get("f6")),
        "amplitude": to_float(item.get("f7")),
        "turnover_rate": to_float(item.get("f8")),
        "pe_dynamic": to_float(item.get("f9")),
        "volume_ratio": to_float(item.get("f10")),
        "high": to_float(item.get("f15")),
        "low": to_float(item.get("f16")),
        "open": to_float(item.get("f17")),
        "prev_close": to_float(item.get("f18")),
        "total_market_cap": to_float(item.get("f20")),
        "float_market_cap": to_float(item.get("f21")),
        "pb": to_float(item.get("f23")),
    }


def fetch_eastmoney_page(
    session: requests.Session,
    page: int,
    page_size: int,
) -> Tuple[List[Dict[str, Any]], int]:
    """请求东方财富的一页股票列表，返回 (数据列表, 总条数)。"""
    params = dict(EASTMONEY_BASE_PARAMS)
    params.update(
        {
            "pn": page,
            "pz": page_size,
            "_": int(time.time() * 1000),
        }
    )

    last_error: Optional[Exception] = None
    for host in EASTMONEY_HOSTS:
        url = host + EASTMONEY_PATH
        try:
            response = session.get(url, params=params, timeout=20)
            response.raise_for_status()
            response.encoding = "utf-8"
            payload = json.loads(response.text)

            if payload.get("rc") != 0:
                raise ValueError(f"接口返回异常：rc={payload.get('rc')}")

            data = payload.get("data") or {}
            diff = data.get("diff") or []
  
            if isinstance(diff, dict):
                diff = list(diff.values())
            return diff, int(data.get("total") or 0)
        except (requests.RequestException, ValueError, TypeError) as exc:
            last_error = exc

    raise RuntimeError(f"东方财富接口请求失败：{last_error}")


def crawl_eastmoney(
    session: requests.Session,
    max_pages: int,
    page_size: int,
) -> Tuple[List[Dict[str, Any]], int]:
    """按页抓取东方财富股票列表，max_pages=0 表示抓取全部。"""
    records: List[Dict[str, Any]] = []
    total = 0
    page = 1

    while True:
        diff, total = fetch_eastmoney_page(session, page, page_size)
        for item in diff:
            record = normalize_eastmoney(item)
            if record is not None:
                records.append(record)

        print(f"  东方财富第 {page} 页：{len(diff)} 条，累计 {len(records)} 条")
        if not diff:
            break
        if total and page * page_size >= total:
            break
        if len(diff) < page_size:
            break
        if max_pages > 0 and page >= max_pages:
            break

        page += 1
        time.sleep(0.5)  

    if not records:
        raise RuntimeError("东方财富接口未返回有效股票数据")
    return records, total


def fetch_sina_total(session: requests.Session) -> int:
    """获取新浪股票列表的总条数。"""
    response = session.get(
        SINA_COUNT_URL,
        params={"node": "hs_a"},
        headers={"Referer": "https://finance.sina.com.cn/stock/"},
        timeout=20,
    )
    response.raise_for_status()
    payload = json.loads(response.text)
    return int(payload)


def fetch_sina_page(
    session: requests.Session,
    page: int,
    page_size: int,
) -> List[Dict[str, Any]]:
    """请求新浪股票列表的一页数据。"""
    params = {
        "page": page,
        "num": page_size,
        "sort": "changepercent",  
        "asc": 0,
        "node": "hs_a",
        "symbol": "",
        "_s_r_a": "page",
        "_": int(time.time() * 1000),
    }
    response = session.get(
        SINA_LIST_URL,
        params=params,
        headers={"Referer": "https://finance.sina.com.cn/stock/"},
        timeout=20,
    )
    response.raise_for_status()
    payload = json.loads(response.text)
    if not isinstance(payload, list):
        raise ValueError("新浪接口返回格式不是 JSON 数组")
    return payload


def normalize_sina(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把新浪接口的一条原始 JSON 数据转换为统一字段。"""
    symbol = str(item.get("symbol") or "").strip().lower()
    code = str(item.get("code") or symbol[2:]).strip()
    name = str(item.get("name") or "").strip()
    if not code or not name:
        return None

    prefix = symbol[:2]
    market = {"sh": "沪市", "sz": "深市", "bj": "北交所"}.get(prefix, "其他")

    high = to_float(item.get("high"))
    low = to_float(item.get("low"))
    prev_close = to_float(item.get("settlement"))
    amplitude = None
    if high is not None and low is not None and prev_close:
        amplitude = round((high - low) / prev_close * 100, 2)

    total_cap = to_float(item.get("mktcap"))
    float_cap = to_float(item.get("nmc"))

    return {
        "code": code,
        "name": name,
        "market": market,
        "source": "sina",
        "latest_price": to_float(item.get("trade")),
        "change_percent": to_float(item.get("changepercent")),
        "change_amount": to_float(item.get("pricechange")),
        "volume": to_float(item.get("volume")),
        "turnover": to_float(item.get("amount")),
        "amplitude": amplitude,
        "turnover_rate": to_float(item.get("turnoverratio")),
        "pe_dynamic": to_float(item.get("per")),
        "volume_ratio": None,  
        "high": high,
        "low": low,
        "open": to_float(item.get("open")),
        "prev_close": prev_close,
        "total_market_cap": total_cap * 10000 if total_cap is not None else None,
        "float_market_cap": float_cap * 10000 if float_cap is not None else None,
        "pb": to_float(item.get("pb")),
    }


def crawl_sina(
    session: requests.Session,
    max_pages: int,
    page_size: int,
) -> Tuple[List[Dict[str, Any]], int]:
    """按页抓取新浪股票列表，max_pages=0 表示抓取全部。"""
    total = fetch_sina_total(session)
    total_pages = math.ceil(total / page_size)
    pages = total_pages if max_pages <= 0 else min(max_pages, total_pages)

    records: List[Dict[str, Any]] = []
    for page in range(1, pages + 1):
        rows = fetch_sina_page(session, page, page_size)
        for item in rows:
            record = normalize_sina(item)
            if record is not None:
                records.append(record)
        print(f"  新浪第 {page}/{pages} 页：{len(rows)} 条，累计 {len(records)} 条")
        time.sleep(0.5)  

    if not records:
        raise RuntimeError("新浪接口未返回有效股票数据")
    return records, total


def save_stocks(
    conn: sqlite3.Connection,
    records: List[Dict[str, Any]],
    updated_at: str,
) -> None:
    """批量写入股票行情；同一代码执行更新，避免重复数据。"""
    placeholders = ", ".join("?" for _ in STOCK_COLUMNS) + ", ?"
    update_clause = ", ".join(
        f"{column} = excluded.{column}" for column in STOCK_COLUMNS[1:]
    )
    sql = f"""
    INSERT INTO stocks ({", ".join(STOCK_COLUMNS)}, updated_at)
    VALUES ({placeholders})
    ON CONFLICT (code) DO UPDATE SET
        {update_clause},
        updated_at = excluded.updated_at
    """

    values = [
        tuple(record.get(column) for column in STOCK_COLUMNS) + (updated_at,)
        for record in records
    ]
    with conn:
        conn.executemany(sql, values)


def print_summary(conn: sqlite3.Connection) -> None:
    """打印涨幅前 10 名，检查入库结果。"""
    total = conn.execute("SELECT COUNT(*) AS n FROM stocks").fetchone()["n"]
    print(f"\n入库完成，stocks 表共有 {total} 条记录。")

    rows = conn.execute(
        """
        SELECT code, name, latest_price, change_percent, turnover, source
        FROM stocks
        WHERE change_percent IS NOT NULL
        ORDER BY change_percent DESC
        LIMIT 10
        """
    ).fetchall()

    print(f"{'代码':<8}{'名称':<12}{'最新价':>9}"
          f"{'涨跌幅%':>10}{'成交额(亿)':>12}  来源")
    print("-" * 66)
    for row in rows:
        price = row["latest_price"]
        change = row["change_percent"]
        turnover = row["turnover"]
        price_text = f"{price:.2f}" if price is not None else "-"
        change_text = f"{change:.2f}" if change is not None else "-"
        turnover_text = f"{turnover / 1e8:.2f}" if turnover is not None else "-"
        print(
            f"{row['code']:<8}{row['name']:<12}"
            f"{price_text:>9}{change_text:>10}{turnover_text:>12}  "
            f"{row['source']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="股票行情定向爬虫")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(__file__).resolve().with_name("stocks.db"),
        help="SQLite 数据库路径，默认与脚本同目录",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=5,
        help="抓取页数，0 表示抓取全部，默认 5 页",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="每页条数，默认 100",
    )
    parser.add_argument(
        "--source",
        choices=("auto", "eastmoney", "sina"),
        default="auto",
        help="数据源：auto 优先东方财富，失败后自动切换新浪",
    )
    args = parser.parse_args()

    if args.pages < 0:
        parser.error("--pages 不能为负数")
    if args.page_size <= 0:
        parser.error("--page-size 必须大于 0")

    conn = connect_db(args.db.resolve())
    session = build_session()

    try:
        if args.source == "eastmoney":
            records, total = crawl_eastmoney(session, args.pages, args.page_size)
        elif args.source == "sina":
            records, total = crawl_sina(session, args.pages, args.page_size)
        else:
            try:
                print("尝试使用东方财富接口 ...")
                records, total = crawl_eastmoney(
                    session, args.pages, args.page_size
                )
            except Exception as exc:  
                print(f"东方财富接口不可用（{type(exc).__name__}），"
                      f"原因：{str(exc)[:160]}")
                print("自动切换到新浪股票接口 ...")
                records, total = crawl_sina(session, args.pages, args.page_size)

        updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        save_stocks(conn, records, updated_at)
        print(f"接口报告总数：{total}，本次写入/更新：{len(records)} 条")
        print_summary(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

