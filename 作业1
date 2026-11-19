#!/usr/bin/env python3
# -*- coding: utf-8 -*-


from __future__ import annotations

import argparse
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


CITIES: Dict[str, str] = {
    "101010100": "北京",
    "101020100": "上海",
    "101280101": "广州",
    "101280601": "深圳",
    "101270101": "成都",
}

BASE_URL = "http://www.weather.com.cn/weather/{city_id}.shtml"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "http://www.weather.com.cn/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS cities (
    city_id   TEXT PRIMARY KEY,
    city_name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS weather_forecast (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    city_id        TEXT NOT NULL,
    city_name      TEXT NOT NULL,
    forecast_date  TEXT NOT NULL,
    week           TEXT,
    weather        TEXT,
    temp_high      INTEGER,
    temp_low       INTEGER,
    wind_direction TEXT,
    wind_level     TEXT,
    source_url     TEXT,
    crawled_at     TEXT NOT NULL,
    UNIQUE (city_id, forecast_date),
    FOREIGN KEY (city_id) REFERENCES cities (city_id)
);

CREATE INDEX IF NOT EXISTS idx_weather_date
    ON weather_forecast (forecast_date);
"""


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
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def to_int(text: Optional[str]) -> Optional[int]:
    """从包含单位的中文文本中提取整数，例如 “-2℃” -> -2。"""
    if not text:
        return None
    match = re.search(r"-?\d+", text.replace("−", "-"))
    return int(match.group()) if match else None


def resolve_forecast_date(day: int, today: date, index: int) -> date:
    """
    根据网页中的“日”数字推算完整日期。

    7 日预报可能跨月，因此从今天开始向后寻找第一个日号相同的日期；
    若匹配失败，则退化为“今天 + 序号”。
    """
    for offset in range(0, 8):
        current = today + timedelta(days=offset)
        if current.day == day:
            return current
    return today + timedelta(days=index)


def parse_forecast_page(
    html: str,
    city_id: str,
    city_name: str,
    source_url: str,
) -> List[Dict[str, object]]:
    """解析天气页面中的 7 日预报列表，返回待入库的记录。"""
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select("ul.t.clearfix > li")
    if not items:
        raise ValueError("页面结构可能已变化：未找到 7 日预报列表")

    today = date.today()
    crawled_at = datetime.now().astimezone().isoformat(timespec="seconds")
    records: List[Dict[str, object]] = []

    for index, item in enumerate(items):
        title = item.find("h1")
        if title is None:
            continue

        label = title.get_text(strip=True) 
        day_match = re.search(r"(\d{1,2})日", label)
        if day_match is None:
            continue

        day = int(day_match.group(1))
        week_match = re.search(r"[（(]([^）)]+)[）)]", label)
        week = week_match.group(1).strip() if week_match else ""

        weather_node = item.select_one("p.wea")
        weather = ""
        if weather_node is not None:
            weather = weather_node.get("title") or weather_node.get_text(strip=True)

        temp_high: Optional[int] = None
        temp_low: Optional[int] = None
        temp_node = item.select_one("p.tem")
        if temp_node is not None:
            high_node = temp_node.find("span")
            low_node = temp_node.find("i")
            if high_node is not None:
                temp_high = to_int(high_node.get_text(strip=True))
            if low_node is not None:
                temp_low = to_int(low_node.get_text(strip=True))

        wind_level = ""
        wind_direction = ""
        wind_node = item.select_one("p.win")
        if wind_node is not None:
            level_node = wind_node.find("i")
            if level_node is not None:
                wind_level = level_node.get_text(strip=True)

            directions: List[str] = []
            for span in wind_node.select("em span[title]"):
                direction = (span.get("title") or "").strip()
                if direction and direction not in directions:
                    directions.append(direction)
            wind_direction = "转".join(directions)

        records.append(
            {
                "city_id": city_id,
                "city_name": city_name,
                "forecast_date": resolve_forecast_date(day, today, index).isoformat(),
                "week": week,
                "weather": weather,
                "temp_high": temp_high,
                "temp_low": temp_low,
                "wind_direction": wind_direction,
                "wind_level": wind_level,
                "source_url": source_url,
                "crawled_at": crawled_at,
            }
        )

    if not records:
        raise ValueError("未解析到任何预报数据")
    return records


def crawl_city(
    session: requests.Session,
    city_id: str,
    city_name: str,
) -> List[Dict[str, object]]:
    """抓取并解析单个城市的 7 日天气预报。"""
    url = BASE_URL.format(city_id=city_id)
    response = session.get(url, timeout=20)
    response.raise_for_status()
    response.encoding = "utf-8"
    return parse_forecast_page(response.text, city_id, city_name, url)


def save_forecasts(
    conn: sqlite3.Connection,
    city_id: str,
    city_name: str,
    records: List[Dict[str, object]],
) -> None:
    """将城市及其预报写入数据库；同一城市同一天的数据执行更新。"""
    sql = """
    INSERT INTO weather_forecast (
        city_id, city_name, forecast_date, week, weather,
        temp_high, temp_low, wind_direction, wind_level,
        source_url, crawled_at
    ) VALUES (
        :city_id, :city_name, :forecast_date, :week, :weather,
        :temp_high, :temp_low, :wind_direction, :wind_level,
        :source_url, :crawled_at
    )
    ON CONFLICT (city_id, forecast_date) DO UPDATE SET
        city_name      = excluded.city_name,
        week           = excluded.week,
        weather        = excluded.weather,
        temp_high      = excluded.temp_high,
        temp_low       = excluded.temp_low,
        wind_direction = excluded.wind_direction,
        wind_level     = excluded.wind_level,
        source_url     = excluded.source_url,
        crawled_at     = excluded.crawled_at;
    """
    with conn:
        conn.execute(
            """
            INSERT INTO cities (city_id, city_name)
            VALUES (?, ?)
            ON CONFLICT (city_id) DO UPDATE SET city_name = excluded.city_name
            """,
            (city_id, city_name),
        )
        conn.executemany(sql, records)


def print_summary(conn: sqlite3.Connection) -> None:
    """在控制台打印入库结果，便于检查。"""
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM weather_forecast"
    ).fetchone()["n"]
    print(f"\n入库完成，weather_forecast 表共有 {total} 条记录。")

    rows = conn.execute(
        """
        SELECT city_name, forecast_date, week, weather,
               temp_high, temp_low, wind_direction, wind_level
        FROM weather_forecast
        ORDER BY city_name, forecast_date
        LIMIT 15
        """
    ).fetchall()

    print(f"{'城市':<6}{'日期':<12}{'星期':<7}{'天气':<10}"
          f"{'最高':>5}{'最低':>5}  {'风向':<10}{'风力'}")
    print("-" * 78)
    for row in rows:
        print(
            f"{row['city_name']:<6}"
            f"{row['forecast_date']:<12}"
            f"{row['week']:<7}"
            f"{row['weather']:<10}"
            f"{row['temp_high'] if row['temp_high'] is not None else '-':>5}"
            f"{row['temp_low'] if row['temp_low'] is not None else '-':>5}  "
            f"{row['wind_direction']:<10}"
            f"{row['wind_level']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="中国气象网 7 日天气预报爬虫")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(__file__).resolve().with_name("weather.db"),
        help="SQLite 数据库路径，默认与脚本同目录",
    )
    args = parser.parse_args()

    conn = connect_db(args.db.resolve())
    session = build_session()
    success = 0

    try:
        for index, (city_id, city_name) in enumerate(CITIES.items(), start=1):
            print(f"[{index}/{len(CITIES)}] 正在抓取 {city_name} ...")
            try:
                records = crawl_city(session, city_id, city_name)
                save_forecasts(conn, city_id, city_name, records)
                success += 1
                print(f"    OK：解析到 {len(records)} 天预报")
            except (requests.RequestException, ValueError) as exc:
                print(f"    FAIL：{exc}")
            time.sleep(0.6)  

        print_summary(conn)
        if success == 0:
            raise SystemExit("所有城市均抓取失败，请检查网络或页面结构。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
