#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


API_URL = "https://www.shanghairanking.cn/api/pub/v1/bcur"
PAGE_URL = "https://www.shanghairanking.cn/rankings/bcur/2021"
YEAR = 2021
BCUR_TYPE = 11  

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": PAGE_URL,
    "Accept": "application/json, text/plain, */*",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS universities (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    year              INTEGER NOT NULL,
    ranking           TEXT NOT NULL,       -- 榜单名次，保留原始文本
    university_name   TEXT NOT NULL,
    university_name_en TEXT,
    province          TEXT,
    category          TEXT,
    score             REAL,
    tags              TEXT,                -- JSON 数组字符串
    crawled_at        TEXT NOT NULL,
    UNIQUE (year, university_name)
);

CREATE INDEX IF NOT EXISTS idx_universities_rank
    ON universities (year, CAST(ranking AS INTEGER));
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
    conn.executescript(SCHEMA)
    return conn


def to_float(value: Any) -> Optional[float]:
    """安全转换为 float，空值或异常值返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if text in {"", "-", "--", "null", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def fetch_rankings(session: requests.Session) -> List[Dict[str, Any]]:
    """调用软科公开 API，获取 2021 主榜全部院校的 JSON 数据。"""
    params = {"bcur_type": BCUR_TYPE, "year": YEAR}
    response = session.get(API_URL, params=params, timeout=30)
    response.raise_for_status()
    response.encoding = "utf-8"

    payload = json.loads(response.text)
    if payload.get("code") != 200:
        raise ValueError(f"接口返回异常：{payload.get('msg')}")

    rankings = (payload.get("data") or {}).get("rankings") or []
    if not rankings:
        raise ValueError("接口未返回院校数据")
    return rankings


def normalize(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把一条 API 记录转换为数据库字段。"""
    name = str(item.get("univNameCn") or "").strip()
    if not name:
        return None

    ranking = str(item.get("ranking") or "").strip()
    if not ranking:
        ranking = str(item.get("rankOverall") or "").strip()
    if not ranking:
        return None

    tags = item.get("univTags") or []
    if not isinstance(tags, list):
        tags = []

    return {
        "year": YEAR,
        "ranking": ranking,
        "university_name": name,
        "university_name_en": (item.get("univNameEn") or "").strip(),
        "province": (item.get("province") or "").strip(),
        "category": (item.get("univCategory") or "").strip(),
        "score": to_float(item.get("score")),
        "tags": json.dumps(tags, ensure_ascii=False),
    }


def save_rankings(
    conn: sqlite3.Connection,
    records: List[Dict[str, Any]],
    crawled_at: str,
) -> None:
    """批量写入院校排名；同一年、同一学校执行更新。"""
    sql = """
    INSERT INTO universities (
        year, ranking, university_name, university_name_en,
        province, category, score, tags, crawled_at
    ) VALUES (
        :year, :ranking, :university_name, :university_name_en,
        :province, :category, :score, :tags, :crawled_at
    )
    ON CONFLICT (year, university_name) DO UPDATE SET
        ranking            = excluded.ranking,
        university_name_en = excluded.university_name_en,
        province           = excluded.province,
        category           = excluded.category,
        score              = excluded.score,
        tags               = excluded.tags,
        crawled_at         = excluded.crawled_at;
    """
    values = [dict(record, crawled_at=crawled_at) for record in records]
    with conn:
        conn.executemany(sql, values)


def print_summary(conn: sqlite3.Connection) -> None:
    """打印前 10 名和总数，检查入库结果。"""
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM universities WHERE year = ?", (YEAR,)
    ).fetchone()["n"]
    print(f"\n入库完成，universities 表中共有 {YEAR} 年院校 {total} 所。")

    rows = conn.execute(
        """
        SELECT ranking, university_name, province, category, score
        FROM universities
        WHERE year = ?
        ORDER BY CAST(ranking AS INTEGER), ranking, university_name
        LIMIT 10
        """,
        (YEAR,),
    ).fetchall()

    print(f"{'排名':<6}{'学校':<16}{'省市':<8}{'类型':<8}{'总分':>8}")
    print("-" * 52)
    for row in rows:
        score = f"{row['score']:.1f}" if row["score"] is not None else "-"
        print(
            f"{row['ranking']:<6}"
            f"{row['university_name']:<16}"
            f"{row['province']:<8}"
            f"{row['category']:<8}"
            f"{score:>8}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="软科中国大学 2021 主榜爬虫")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(__file__).resolve().with_name("universities.db"),
        help="SQLite 数据库路径，默认与脚本同目录",
    )
    args = parser.parse_args()

    conn = connect_db(args.db.resolve())
    session = build_session()

    try:
        print(f"正在请求 {YEAR} 年软科中国大学主榜接口 ...")
        rankings = fetch_rankings(session)

        records = []
        for item in rankings:
            record = normalize(item)
            if record is not None:
                records.append(record)

        if not records:
            raise SystemExit("没有可入库的院校数据")

        crawled_at = datetime.now().astimezone().isoformat(timespec="seconds")
        save_rankings(conn, records, crawled_at)
        print(f"接口返回 {len(rankings)} 条，成功整理 {len(records)} 条。")
        print_summary(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
