# app.py  (硬核版：重试 + 退避 + 本地兜底缓存 + 更稳解析 + 清晰报错)
import re
import ast
import json
import time
from pathlib import Path
from datetime import datetime

import requests
import pandas as pd
import streamlit as st
import statistics

st.set_page_config(page_title="个人基金助手（硬核版）", layout="wide")

# -------------------- 基本配置 --------------------
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Connection": "close",
}

LIST_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
GZ_URL = "https://fundgz.1234567.com.cn/js/{}.js"
NAV_URL = "https://fundf10.eastmoney.com/F10DataApi.aspx?type=lsjz&code={}&page=1&per=200"

CACHE_DIR = Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)
FUNDS_CACHE = CACHE_DIR / "funds_cache.json"

# 降低被限频概率：同一函数内请求间隔（秒）
SOFT_SLEEP = 0.25


# -------------------- 网络层：重试 + 退避 + 超时 --------------------
def _safe_get(url: str, timeout: int = 12, retries: int = 4) -> requests.Response:
    """
    稳健 GET：超时、重试、退避、明确抛错
    """
    last_err = None
    for k in range(retries):
        try:
            # 轻微抑制频率
            if k > 0:
                time.sleep(0.8 * k)  # 线性退避
            r = requests.get(url, headers=UA, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last_err = e
    raise last_err


def _json_dump(path: Path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _json_load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# -------------------- 数据：基金列表（带本地兜底） --------------------
def _parse_fund_list(js_text: str):
    """
    解析 fundcode_search.js
    典型：var r = [["000001","...", "名称","..."], ...];
    """
    m = re.search(r"var\s+r\s*=\s*(\[\[.*?\]\]);", js_text, re.S)
    if not m:
        raise ValueError("没有匹配到基金列表 var r = [[...]]（可能被拦截或页面改版）")
    try:
        data = ast.literal_eval(m.group(1))
    except Exception as e:
        raise ValueError(f"基金列表 literal_eval 解析失败：{e}")
    if not isinstance(data, list) or len(data) < 1000:
        # 正常量很大（几千到上万），小得离谱多半是被拦截返回异常内容
        raise ValueError("基金列表数据量异常偏小（可能返回了错误页/被拦截）")
    return data


@st.cache_data(ttl=86400)
def load_funds_hardened():
    """
    优先在线拉取并刷新本地缓存；在线失败则读本地缓存。
    """
    # 1) 尝试在线获取
    try:
        r = _safe_get(LIST_URL, timeout=15, retries=4)
        data = _parse_fund_list(r.text)
        # 刷新本地兜底缓存
        _json_dump(FUNDS_CACHE, {"ts": datetime.utcnow().isoformat(), "data": data})
        return data, "online"
    except Exception as e_online:
        # 2) 在线失败 -> 读本地缓存兜底
        if FUNDS_CACHE.exists():
            try:
                cached = _json_load(FUNDS_CACHE)
                data = cached.get("data", [])
                if isinstance(data, list) and len(data) > 1000:
                    return data, f"cache（在线失败：{e_online}）"
            except Exception:
                pass
        # 3) 缓存也失败 -> 抛错
        raise RuntimeError(f"基金列表加载失败（在线失败且无可用缓存）：{e_online}")


# -------------------- 数据：估值 --------------------
@st.cache_data(ttl=30)
def get_gz(code: str):
    """
    天天基金估值：fundgz
    可能格式：jsonpgz({...}); 或 callback({...});
    """
    code = str(code).strip()
    if not code.isdigit():
        return None

    try:
        time.sleep(SOFT_SLEEP)
        r = _safe_get(GZ_URL.format(code), timeout=10, retries=3)
        m = re.search(r"\((\{.*\})\)", r.text, re.S)
        if not m:
            return None
        return json.loads(m.group(1))
    except Exception:
        return None


# -------------------- 数据：历史净值 --------------------
def _parse_nav_tables(html_text: str):
    """
    从 F10DataApi 的 HTML 表格里取净值数据
    """
    tables = pd.read_html(html_text)
    if not tables:
        return []

    df = tables[0].copy()
    need_cols = {"净值日期", "单位净值"}
    if not need_cols.issubset(set(df.columns)):
        return []

    out = []
    for _, row in df.iterrows():
        d = str(row["净值日期"]).strip()
        v = row["单位净值"]
        try:
            v = float(v)
        except Exception:
            continue
        out.append((d, v))

    # 通常返回倒序（新到旧）
    out.reverse()
    return out


@st.cache_data(ttl=3600)
def get_nav(code: str):
    code = str(code).strip()
    if not code.isdigit():
        return []

    try:
        time.sleep(SOFT_SLEEP)
        r = _safe_get(NAV_URL.format(code), timeout=15, retries=4)
        nav = _parse_nav_tables(r.text)
        return nav
    except Exception:
        return []


# -------------------- 风险评分（波动率 + 最大回撤） --------------------
def risk(nav):
    if len(nav) < 30:
        return 50, "观望（数据不足）", 0.0, 0.0

    rets = []
    for i in range(1, len(nav)):
        prev = nav[i - 1][1]
        cur = nav[i][1]
        if prev <= 0:
            continue
        rets.append(cur / prev - 1)

    if len(rets) < 10:
        return 50, "观望（样本不足）", 0.0, 0.0

    vol = statistics.pstdev(rets)  # 波动率
    peak = nav[0][1]
    dd = 0.0
    for _, v in nav:
        if v > peak:
            peak = v
        if peak > 0:
            dd = max(dd, (peak - v) / peak)

    # 你的原始思路系数：v*4000 + dd*200
    score = int(vol * 4000 + dd * 200)
    score = max(0, min(100, score))

    if score > 70:
        action = "减仓"
    elif score < 35:
        action = "加仓"
    else:
        action = "观望"

    return score, action, vol, dd


# -------------------- UI --------------------
st.title("📊 个人基金助手（硬核版）")

with st.sidebar:
    menu = st.radio("菜单", ["搜索", "详情", "诊断"])
    st.caption("提示：数据源可能限频/反爬；本版本内置重试与基金列表本地兜底缓存。")

# 加载基金列表
try:
    funds, source = load_funds_hardened()
except Exception as e:
    st.error(str(e))
    st.stop()

st.caption(f"基金列表来源：**{source}**（本地缓存文件：{FUNDS_CACHE.as_posix()}）")

if menu == "搜索":
    st.subheader("搜索基金")
    q = st.text_input("输入基金代码/名称/拼音（包含匹配）", placeholder="例如：161725 或 半导体 或 hs300")

    if q:
        q = q.strip()
        rows = []
        q_low = q.lower()

        for row in funds:
            code = str(row[0]) if len(row) > 0 else ""
            # 常见：row[2]=名称 row[1]=简拼/拼音 row[3]=扩展
            pinyin = str(row[1]) if len(row) > 1 else ""
            name = str(row[2]) if len(row) > 2 else ""
            extra = str(row[3]) if len(row) > 3 else ""

            hit = (q in code) or (q in name) or (q_low in pinyin.lower()) or (q_low in extra.lower())
            if hit:
                rows.append({"代码": code, "名称": name, "简拼": pinyin, "备注": extra})

        if rows:
            st.dataframe(pd.DataFrame(rows).head(120), use_container_width=True)
        else:
            st.info("没搜到匹配项（可能输入太短/太偏）")

if menu == "详情":
    st.subheader("基金详情")
    code = st.text_input("输入基金代码", placeholder="例如：161725")

    if code:
        code = code.strip()
        colA, colB = st.columns([1, 2], vertical_alignment="top")

        with colA:
            gz = get_gz(code)
            if gz:
                st.metric("估值(gsz)", gz.get("gsz", "-"), f'{gz.get("gszzl","-")} %')
                st.caption(f'更新时间：{gz.get("gsrq","")} {gz.get("gstime","")}')
                st.caption(f'基金：{gz.get("name","-")}（{gz.get("fundcode","-")}）')
            else:
                st.warning("估值接口暂无数据（可能不支持/限频/被拦截）。不会影响净值与风险计算。")

            nav = get_nav(code)
            if not nav:
                st.error("❌ 历史净值获取失败：可能代码不对、接口限频、或被拦截。")
                st.stop()

            score, action, vol, dd = risk(nav)
            st.write(f"**风险分**：{score}/100")
            st.write(f"**建议**：{action}")
            st.caption(f"波动率(标准差)：{vol:.6f}；最大回撤：{dd*100:.2f}%")

            st.divider()
            st.caption("说明：风险分=波动率×4000 + 最大回撤×200（截断到0-100），仅作参考。")

        with colB:
            df = pd.DataFrame(nav, columns=["日期", "净值"])
            df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
            df = df.dropna().sort_values("日期")

            st.line_chart(df.set_index("日期")["净值"], use_container_width=True)
            st.dataframe(df.tail(40), use_container_width=True)

if menu == "诊断":
    st.subheader("网络/数据源诊断（你一眼看出是哪一步挂了）")

    cols = st.columns(3)
    with cols[0]:
        if st.button("测试：基金列表"):
            try:
                r = _safe_get(LIST_URL, timeout=12, retries=2)
                _ = _parse_fund_list(r.text)
                st.success("基金列表 OK")
            except Exception as e:
                st.error(f"基金列表失败：{e}")

    with cols[1]:
        test_code = st.text_input("测试估值代码", value="161725")
        if st.button("测试：估值"):
            try:
                gz = get_gz(test_code)
                if gz:
                    st.success(f"估值 OK：gsz={gz.get('gsz')} gszzl={gz.get('gszzl')}")
                else:
                    st.warning("估值返回空：可能限频/不支持/被拦截")
            except Exception as e:
                st.error(f"估值失败：{e}")

    with cols[2]:
        test_code2 = st.text_input("测试净值代码", value="161725")
        if st.button("测试：历史净值"):
            nav = get_nav(test_code2)
            if nav:
                st.success(f"历史净值 OK：条数={len(nav)}，最近={nav[-1]}")
            else:
                st.error("历史净值失败：可能限频/被拦截/代码不对")

    st.divider()
    st.caption("如果你部署在云上经常失败：通常是云IP被限频。解决路线：降低请求频率/加代理/把基金列表做离线文件。")
