import re
import time
import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="基金助手", layout="wide")
st.title("📊 个人基金助手")

UA = {"User-Agent": "Mozilla/5.0"}

LIST_URL = "http://fund.eastmoney.com/js/fundcode_search.js"
GZ_URL = "https://fundgz.1234567.com.cn/js/{}.js"
NAV_URL = "https://fundf10.eastmoney.com/F10DataApi.aspx?type=lsjz&code={}&page=1&per=200"


@st.cache_data(ttl=86400)
def load_funds():
    r = requests.get(LIST_URL, headers=UA)
   import ast

m = re.search(r"var r\s*=\s*(\[\[.*?\]\]);", r.text, re.S)

if not m:
    st.error("❌ 取不到基金数据，网站可能封IP了")
    st.stop()

return ast.literal_eval(m.group(1))

@st.cache_data(ttl=30)
def get_gz(code):
    try:
        r = requests.get(GZ_URL.format(code), headers=UA)
        j = re.search(r"\\((.*)\\)", r.text).group(1)

        import json
        return json.loads(j)
    except:
        return None


@st.cache_data(ttl=3600)
def get_nav(code):
    r = requests.get(NAV_URL.format(code), headers=UA)
    df = pd.read_html(r.text)[0]

    out = []

    for _, i in df.iterrows():
        out.append((i["净值日期"], float(i["单位净值"])))

    return out[::-1]


def risk(nav):
    if len(nav) < 30:
        return 50, "观望"

    ret = []

    for i in range(1, len(nav)):
        ret.append(nav[i][1] / nav[i-1][1] - 1)

    import statistics

    v = statistics.pstdev(ret)

    peak = nav[0][1]
    dd = 0

    for i in nav:
        if i[1] > peak:
            peak = i[1]

        d = (peak - i[1]) / peak
        if d > dd:
            dd = d

    s = min(100, int(v*4000 + dd*200))

    if s > 70:
        return s, "减仓"
    if s < 35:
        return s, "加仓"

    return s, "观望"


funds = load_funds()

menu = st.sidebar.radio("菜单", ["搜索", "详情"])


if menu == "搜索":
    q = st.text_input("输入基金代码/名称")

    if q:
        data = []

        for i in funds:
            if q in i[0] or q in i[2]:
                data.append((i[0], i[2], i[3]))

        st.table(data[:50])


if menu == "详情":
    code = st.text_input("基金代码")

    if code:
        gz = get_gz(code)
        nav = get_nav(code)

        if gz:
            st.metric("估值", gz["gsz"], gz["gszzl"])

        s, a = risk(nav)

        st.write("风险：", s, a)

        df = pd.DataFrame(nav, columns=["日期", "净值"])
        df["日期"] = pd.to_datetime(df["日期"])

        st.line_chart(df.set_index("日期"))
