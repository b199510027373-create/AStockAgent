import sys, os, time, json, datetime, requests
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost,::1")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import gradio as gr
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

from astock.data import load_daily as _load_daily
from astock.indicators import add_all as _add_indicators
from astock.ui import build_multiperiod_tab
from astock.backtest_ui import build_backtest_tab
from astock.recommend_ui import build_recommend_tab
from astock.track_ui import build_track_tab
from astock.optimize_ui import build_optimize_tab
from astock.factor_ui import build_factor_tab
from astock.strategy_ui import build_strategy_lab_tab
from astock.scheduler import start_background as start_recommend_scheduler
from astock.events import build_ai_context
from astock.llm import chat as llm_chat

# >>>>>>>>>>>> 配置区 <<<<<<<<<<<<<
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")
DEEPSEEK_KEY  = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE = os.getenv("DEEPSEEK_BASE", "https://api.deepseek.com")
# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>

# ====== 初始化 ======
# 行情数据走 astock.data；DeepSeek 走 astock.llm（直连/代理自动切换）

DATA_CACHE = {}
STOCK_CSV   = os.path.join(BASE_DIR, "stocks.csv")
FILTER_CSV  = os.path.join(BASE_DIR, "filtered_stocks.csv")

# ====== 1. 数据获取 ======
def get_df(ts_code):
    """统一走 astock 数据层：绕过系统代理、带本地缓存。"""
    if ts_code in DATA_CACHE and time.time() - DATA_CACHE[ts_code][0] < 300:
        return DATA_CACHE[ts_code][1].copy()
    try:
        today = datetime.datetime.now().strftime("%Y%m%d")
        start = (datetime.datetime.now() - datetime.timedelta(days=180)).strftime("%Y%m%d")
        df = _load_daily(ts_code, start=start, end=today, adj="qfq")
    except Exception:
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.sort_values("trade_date").reset_index(drop=True)
    DATA_CACHE[ts_code] = (time.time(), df.copy())
    return df

# ====== 2. 指标计算（统一走 astock.indicators）======
def ind(df):
    """兼容遗留字段名；唯一实现是 astock.indicators.add_all。"""
    if df is None or df.empty or len(df) < 60:
        return pd.DataFrame()
    out = _add_indicators(df).copy()
    aliases = {
        "rsi": "rsi14",
        "macd": "dif",
        "macd_sig": "dea",
        "middle_band": "boll_mid",
        "upper_band": "boll_upper",
        "lower_band": "boll_lower",
        "cci": "cci20",
        "atr": "atr14",
        "vr": "vr26",
        "psy": "psy12",
    }
    for old, new in aliases.items():
        if new in out.columns:
            out[old] = out[new]
    # 遗留图只依赖这些列；不能因 ma250 尚未形成而把整段历史删光
    subset = [c for c in ("ma20", "rsi", "macd", "atr", "cci", "vr") if c in out.columns]
    return out.dropna(subset=subset).reset_index(drop=True)

# ====== 3. 图形策略识别 ======
def detect_patterns(df):
    if len(df) < 20: return "数据不足"
    L = df.iloc[-1]
    patterns = []
    
    if L['close'] > L['ma20'] and L['rsi'] > 60:
        patterns.append("📈 **强势多头**：股价站上MA20，RSI强势")
    if L['close'] < L['ma60'] and L['rsi'] < 40:
        patterns.append("📉 **弱势空头**：股价受压MA60，RSI疲软")
    if L['cci'] > 100:
        patterns.append("🔥 **CCI突破**：进入强势区间")
    if L['vr'] > 150:
        patterns.append("💧 **量能过热**：VR超过150，警惕放量滞涨")
    if L['psy'] > 75:
        patterns.append("🧠 **心理过热**：PSY超买")
        
    return "\n".join(patterns) if patterns else "无明显经典形态"

# ====== 4. DeepSeek 调用 ======
def ds_chat(messages, max_tokens=800):
    try:
        return llm_chat(messages, max_tokens=max_tokens, temperature=0.7)
    except Exception as e:
        return f"❌ DeepSeek 错误: {e}"


def _citation_block(citations):
    if not citations:
        return ""
    lines = ["", "---", "**数据来源**"]
    for i, item in enumerate(citations, 1):
        date = pd.Timestamp(item["date"]).strftime("%Y-%m-%d")
        line = f"[{i}] {date} {item['kind']}：{item['title']}（{item.get('source', '')}）"
        if item.get("url"):
            line += f" [原文]({item['url']})"
        lines.append(line)
    return "\n".join(lines)

# ====== 5. 单股体检（补齐K线图表）=====
def single(ts_code, view_mode="basic"):
    df = get_df(ts_code)
    if df.empty: return "❌ 无数据", go.Figure(), "无形态"
    df = ind(df)
    if df.empty: return "❌ 数据不足", go.Figure(), "无形态"
    L = df.iloc[-1]
    trend = "多头" if L.ma5 > L.ma20 else "空头"
    summary = f"【{ts_code}】收{L.close:.2f} MA5/20/60={L.ma5:.1f}/{L.ma20:.1f}/{L.ma60:.1f} RSI={L.rsi:.1f}"
    try:
        news_ctx, citations = build_ai_context(ts_code, limit=4)
    except Exception:
        news_ctx, citations = "", []
    user_content = summary + (f"\n\n{news_ctx}" if news_ctx else "")
    ai = ds_chat([
        {"role": "system", "content": "你是A股资深分析师，给操作建议；有参考资料时用[1][2]标注来源，不要编造。"},
        {"role": "user", "content": user_content},
    ])
    ai += _citation_block(citations)
    fig = _plot(df, ts_code) if view_mode == "basic" else _plot_full(df, ts_code)
    pattern_text = detect_patterns(df)
    return f"{summary}\n\n🤖 {ai}", fig, pattern_text
def _plot(df, code):
    """绘制基础K线视图：K线 + MA + 布林带 + 成交量，干净可交互"""
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.65, 0.35]
    )
    fig.add_trace(go.Candlestick(
        x=df.trade_date, open=df.open, high=df.high, low=df.low, close=df.close,
        name="K线", showlegend=False
    ), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.ma5, line=dict(color="#FF8C00", width=1.5), name="MA5"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.ma20, line=dict(color="#2E8B57", width=1.5), name="MA20"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.ma60, line=dict(color="#8B008B", width=1.5), name="MA60"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.upper_band, line=dict(color="rgba(128,128,128,0.5)", width=1, dash="dash"), name="上轨"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.lower_band, line=dict(color="rgba(128,128,128,0.5)", width=1, dash="dash"), name="下轨", fill='tonexty', fillcolor='rgba(128,128,128,0.06)'), row=1, col=1)
    vol_colors = ["#EF5350" if c >= o else "#26A69A" for c, o in zip(df.close, df.open)]
    fig.add_trace(go.Bar(x=df.trade_date, y=df.vol, marker_color=vol_colors, name="成交量", showlegend=False), row=2, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.vol_ma20, line=dict(color="#FFA726", width=1.5, dash="dot"), name="量MA20"), row=2, col=1)
    fig.update_layout(
        title=f"{code} 基础K线图",
        height=650,
        template="plotly_white",
        hovermode="x unified",
        showlegend=True,
        legend=dict(orientation="h", y=1.12, x=0.5, xanchor="center"),
        xaxis_rangeslider_visible=True,
        xaxis_rangeslider_thickness=0.06,
        margin=dict(t=60, b=30, l=50, r=20)
    )
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="成交量", row=2, col=1)
    return fig

def _plot_full(df, code):
    """全指标视图：6行子图"""
    fig = make_subplots(
        rows=6, cols=1, shared_xaxes=True, vertical_spacing=0.02,
        row_heights=[0.40, 0.10, 0.12, 0.12, 0.12, 0.14]
    )
    fig.add_trace(go.Candlestick(x=df.trade_date, open=df.open, high=df.high, low=df.low, close=df.close, name="K线", showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.ma5, line=dict(color="#FF8C00", width=1), name="MA5"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.ma20, line=dict(color="#2E8B57", width=1), name="MA20"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.ma60, line=dict(color="#8B008B", width=1), name="MA60"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.ema12, line=dict(color="#1565C0", width=1, dash="dot"), name="EMA12"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.ema26, line=dict(color="#C62828", width=1, dash="dot"), name="EMA26"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.upper_band, line=dict(color="rgba(128,128,128,0.4)", width=1, dash="dash"), name="上轨"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.lower_band, line=dict(color="rgba(128,128,128,0.4)", width=1, dash="dash"), name="下轨", fill='tonexty', fillcolor='rgba(128,128,128,0.06)'), row=1, col=1)
    vol_colors = ["#EF5350" if c>=o else "#26A69A" for c,o in zip(df.close, df.open)]
    fig.add_trace(go.Bar(x=df.trade_date, y=df.vol, marker_color=vol_colors, name="成交量"), row=2, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.obv, line=dict(color="#00ACC1", width=1), name="OBV"), row=2, col=1)
    macd_colors = ["#EF5350" if v<0 else "#66BB6A" for v in df.macd]
    fig.add_trace(go.Bar(x=df.trade_date, y=df.macd, marker_color=macd_colors, name="MACD"), row=3, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.macd_sig, line=dict(color="#C62828", width=1), name="Signal"), row=3, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.k, line=dict(color="#1565C0", width=1), name="K"), row=4, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.d, line=dict(color="#FF8C00", width=1), name="D"), row=4, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.j, line=dict(color="#8B008B", width=1), name="J"), row=4, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.cci, line=dict(color="#C62828", width=1), name="CCI"), row=5, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.psy, line=dict(color="#2E8B57", width=1), name="PSY"), row=5, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.vr, line=dict(color="#795548", width=1), name="VR"), row=6, col=1)
    fig.add_trace(go.Scatter(x=df.trade_date, y=df.atr, line=dict(color="#424242", width=1), name="ATR"), row=6, col=1)
    fig.update_layout(
        title=f"{code} 全指标技术分析",
        height=1150, template="plotly_white", hovermode="x unified",
        showlegend=True, xaxis_rangeslider_visible=False,
        margin=dict(t=50, b=20, l=50, r=20)
    )
    for i, lbl in enumerate(["价格","量能","MACD","KDJ","CCI/PSY","VR/ATR"], 1):
        fig.update_yaxes(title_text=lbl, row=i, col=1)
    return fig

# ====== 6. 组合体检 ======
def portfolio(text):
    if not text or text.strip() == "": return "⚠️ 请先输入持仓组合（代码:权重）", {}, None
    d = {}
    for x in [s.strip() for s in text.split(",") if s.strip()]:
        cd, w = x.split(":")
        d[cd.strip()] = float(w.strip())
    reps, score, rets = [], 0.0, {}
    for cd, w in d.items():
        df = get_df(cd); df = ind(df)
        if df.empty: reps.append(f"{cd}: 数据不足"); continue
        L = df.iloc[-1]; sc = 6 if L.ma5 > L.ma20 else 4
        if L.rsi < 30: sc += 1
        if L.rsi > 70: sc -= 1
        reps.append(f"{cd}: {'Bull' if sc>5 else 'Bear'} (Score:{sc})")
        score += sc * w
        rets[cd] = df.set_index("trade_date")["close"].pct_change().dropna()
    ai = ds_chat([{"role":"system","content":"你是资产配置专家，点评风险。"}, {"role":"user","content":f"持仓{d}\n{' ; '.join(reps)}\n给总评。"}])
    return f"📊 组合健康度 {score:.1f}/10\n"+"\n".join(reps)+f"\n\n🤖 {ai}", rets, None

def p_charts(rets):
    if not rets: e=go.Figure(); e.update_layout(title="无数据"); return e,e,e
    dfr = pd.DataFrame(rets).sort_index()
    nav = (1 + dfr.fillna(0)).cumprod()
    f1 = go.Figure([go.Scatter(x=nav.index,y=nav[c],mode="lines",name=c) for c in nav.columns]); f1.update_layout(title="净值曲线",template="plotly_white")
    f2 = px.imshow(dfr.corr(),text_auto=".2f",title="相关性热力图",template="plotly_white")
    vol = dfr.std()*np.sqrt(252)
    f3 = go.Figure([go.Bar(x=vol.index,y=vol.values,marker_color="lightblue")]); f3.update_layout(title="年化波动率",template="plotly_white")
    return f1,f2,f3

# ====== 7. 每日简报 ======
def daily_report():
    try: stocks = pd.read_csv(STOCK_CSV)["ts_code"].tolist()
    except: stocks = []
    if not stocks: return "⚠️ 自选股池为空，请先添加股票。"
    parts = []
    for c in stocks[:5]:
        df = get_df(c); df = ind(df)
        if df.empty: continue
        L = df.iloc[-1]
        parts.append(f"{c}: 收{L.close:.2f} MA{L.ma5:.1f}/{L.ma20:.1f} RSI{L.rsi:.1f}")
    if not parts: return "⚠️ 无数据"
    ai = ds_chat([{"role":"system","content":"你是A股投顾。"}, {"role":"user","content":f"今天{datetime.date.today()}\n"+"\n".join(parts)+"\n生成每日简报。"}])
    return f"# 📰 {datetime.date.today()}\n\n## 🤖 AI简报\n{ai}\n\n---\n*非投资建议*"

# ====== 8. 策略选股（全指标）=====
def load_stocks():
    try: return pd.read_csv(STOCK_CSV)["ts_code"].tolist()
    except: return []
def save_stocks(lst): pd.DataFrame({"ts_code":lst}).to_csv(STOCK_CSV,index=False)

def strategy(ma_en, ema_en, rsi_en, macd_en, bb_en, kdj_en, obv_en, cci_en, atr_en, vr_en, psy_en,
             ma_th, ema_th, rsi_low, rsi_high, macd_cond, bb_w, kdj_th, obv_cond, cci_low, cci_high, atr_th, vr_low, vr_high, psy_low, psy_high):
    res = []
    for c in load_stocks():
        df = get_df(c); df = ind(df)
        if df.empty: continue
        L = df.iloc[-1]; ok = True
        
        if ma_en and not (L.ma5 > L.ma20 * ma_th): ok = False
        if ema_en and not (L.ema12 > L.ema26 * ema_th): ok = False
        if rsi_en:
            if rsi_low and L.rsi < rsi_low: ok = False
            if rsi_high and L.rsi > rsi_high: ok = False
        if macd_en:
            if macd_cond == "positive" and L.macd <= 0: ok = False
            if macd_cond == "above_signal" and L.macd <= L.macd_sig: ok = False
        if bb_en and not (L.close < L.lower_band * bb_w): ok = False
        if kdj_en and not (L.j < kdj_th): ok = False
        if obv_en:
            if obv_cond == "rising" and L.obv < df['obv'].iloc[-5]: ok = False
            if obv_cond == "falling" and L.obv > df['obv'].iloc[-5]: ok = False
        if cci_en:
            if cci_low and L.cci < cci_low: ok = False
            if cci_high and L.cci > cci_high: ok = False
        if atr_en and L.atr < atr_th: ok = False
        if vr_en:
            if vr_low and L.vr < vr_low: ok = False
            if vr_high and L.vr > vr_high: ok = False
        if psy_en:
            if psy_low and L.psy < psy_low: ok = False
            if psy_high and L.psy > psy_high: ok = False
            
        if ok:
            res.append(dict(代码=c, 收盘=f"{L.close:.2f}", MA5=f"{L.ma5:.1f}", RSI=f"{L.rsi:.1f}", 
                           MACD=f"{L.macd:.3f}", CCI=f"{L.cci:.1f}", VR=f"{L.vr:.1f}", PSY=f"{L.psy:.1f}"))
    if not res: return "⚠️ 无符合条件的股票", None
    dfr = pd.DataFrame(res)
    dfr.to_csv(FILTER_CSV, index=False, encoding="utf-8-sig")
    return dfr.to_markdown(index=False), FILTER_CSV

# ====== 9. 自选股管理 ======
def add_stock(new, cur):
    if not new: return cur,""
    lst = cur.split(",") if cur else []
    if new not in lst: lst.append(new); save_stocks(lst); return ",".join(lst), f"✅ 已加{new}"
    return cur, f"⚠️ 已存在"
def rm_stock(code, cur):
    lst = cur.split(",") if cur else []
    if code in lst: lst.remove(code); save_stocks(lst); return ",".join(lst), f"✅ 已删{code}"
    return cur, f"⚠️ 无此码"

# ====== 10. AI 对话 ======
def _latest_recommendation_context():
    path = os.path.join(BASE_DIR, "data", "recommendations", "latest.json")
    if not os.path.exists(path):
        return ""
    try:
        data = json.loads(open(path, encoding="utf-8").read())
        rows = data.get("table", [])[:3]
        if not rows:
            return ""
        lines = ["【最新每日推荐摘要】"]
        for row in rows:
            lines.append(
                f"{row.get('代码')} {row.get('名称')} 综合{row.get('综合分')} "
                f"建议{row.get('建议')}：{str(row.get('理由', ''))[:80]}"
            )
        return "\n".join(lines)
    except Exception:
        return ""


def ai_chat(message, history, p_text):
    ctx = f"时间:{datetime.datetime.now():%Y-%m-%d %H:%M}\n自选:{load_stocks()}\n持仓:{p_text}"
    news_ctx, citations = "", []
    try:
        news_ctx, citations = build_ai_context(message, limit=8)
    except Exception as exc:  # noqa: BLE001
        news_ctx = f"（新闻/公告检索失败：{exc}）"
    rec_ctx = _latest_recommendation_context()

    system = (
        "你是A股投顾，回答要区分事实与推断。"
        "如果提供了参考资料，优先使用并用 [1][2] 标注来源；资料不足时明确说明，不要编造新闻、公告或研报。\n"
        f"上下文：{ctx}\n{rec_ctx}\n{news_ctx}"
    )
    msgs = [{"role": "system", "content": system}]
    for m in history:
        msgs.append(m)
    msgs.append({"role": "user", "content": message})
    rep = ds_chat(msgs)
    rep += _citation_block(citations)

    history.extend([{"role": "user", "content": message}, {"role": "assistant", "content": rep}])
    return "", history

# ====== 11. UI ======
with gr.Blocks(title="AStockAgent") as app:
    gr.Markdown("# 📈 AStockAgent 智能投顾（全功能·全指标·真实请求）")
    
    portfolio_in = gr.Textbox(label="持仓 代码:权重", placeholder="例如: 600519.SH:0.6,000001.SZ:0.4", lines=2)
    stocks_cur  = gr.State(",".join(load_stocks()))

    with gr.Tabs():
        # — 单股
        with gr.Tab("🔍 单股"):
            c = gr.Textbox(label="代码", placeholder="例如: 600519.SH")
            view_mode = gr.Radio(["基础视图", "全指标视图"], value="基础视图", label="显示模式")
            b = gr.Button("体检", variant="primary")
            t = gr.Markdown()
            p = gr.Plot()
            pat = gr.Markdown(label="📊 图形策略识别")
            b.click(lambda c, m: single(c, "basic" if m == "基础视图" else "full"), [c, view_mode], [t, p, pat])

        # — 组合
        with gr.Tab("📊 组合"):
            b1 = gr.Button("分析组合", variant="primary")
            b2 = gr.Button("统计图表")
            o = gr.Markdown()
            g1,g2,g3 = gr.Plot(),gr.Plot(),gr.Plot()
            b1.click(lambda x: portfolio(x)[0], portfolio_in, o)
            b2.click(lambda x: p_charts(portfolio(x)[1]), portfolio_in, [g1,g2,g3])

        # — 简报
        with gr.Tab("📰 简报"):
            b = gr.Button("生成简报", variant="primary")
            o = gr.Markdown()
            b.click(daily_report, None, o)

        # — 选股（全指标）
        with gr.Tab("🎯 选股"):
            gr.Markdown("### 条件（基于自选股池）")
            
            with gr.Row():
                ma_cb = gr.Checkbox(label="MA5>MA20", info="短期趋势")
                ema_cb = gr.Checkbox(label="EMA12>EMA26", info="指数平滑趋势")
                rsi_cb = gr.Checkbox(label="RSI过滤", info="相对强弱")
                macd_cb = gr.Checkbox(label="MACD过滤", info="动量指标")
            
            with gr.Row():
                ma_th = gr.Slider(0.98, 1.02, 1.0, 0.01, label="MA阈值")
                ema_th = gr.Slider(0.98, 1.02, 1.0, 0.01, label="EMA阈值")
                rsi_low = gr.Number(label="RSI≥", value=30)
                rsi_high = gr.Number(label="RSI≤", value=70)
            
            with gr.Row():
                macd_cond = gr.Radio(["positive","above_signal"], value="positive", label="MACD条件")
                bb_cb = gr.Checkbox(label="跌破布林下轨", info="价格支撑")
                bb_w = gr.Slider(0.95, 1.05, 1.0, 0.01, label="布林带宽系数")
                kdj_cb = gr.Checkbox(label="KDJ超卖", info="随机指标")
            
            with gr.Row():
                kdj_th = gr.Number(label="J值≤", value=20)
                obv_cb = gr.Checkbox(label="OBV条件", info="能量潮")
                obv_cond = gr.Radio(["rising","falling"], value="rising", label="OBV方向")
                cci_cb = gr.Checkbox(label="CCI过滤", info="商品通道")
            
            with gr.Row():
                cci_low = gr.Number(label="CCI≥", value=-100)
                cci_high = gr.Number(label="CCI≤", value=100)
                atr_cb = gr.Checkbox(label="ATR过滤", info="波动率")
                vr_cb = gr.Checkbox(label="VR过滤", info="成交量变异")
            
            with gr.Row():
                atr_th = gr.Number(label="ATR≥", value=1.0)
                vr_low = gr.Number(label="VR≥", value=40)
                vr_high = gr.Number(label="VR≤", value=150)
                psy_cb = gr.Checkbox(label="PSY过滤", info="心理线")
            
            with gr.Row():
                psy_low = gr.Number(label="PSY≥", value=25)
                psy_high = gr.Number(label="PSY≤", value=75)
            
            bb = gr.Button("🚀 筛选",variant="primary")
            out = gr.Markdown()
            dl = gr.File(label="📥 下载CSV", visible=True)
            
            bb.click(lambda a,b,c,d,e,f,g,h,i,j,k,l,m,n,o,p,q,r,s,t,u,v,w,x,y,z: 
                     strategy(a,b,c,d,e,f,g,h,i,j,k,l,m,n,o,p,q,r,s,t,u,v,w,x,y,z),
                     [ma_cb, ema_cb, rsi_cb, macd_cb, bb_cb, kdj_cb, obv_cb, cci_cb, atr_cb, vr_cb, psy_cb,
                      ma_th, ema_th, rsi_low, rsi_high, macd_cond, bb_w, kdj_th, obv_cond, cci_low, cci_high, atr_th, vr_low, vr_high, psy_low, psy_high],
                     [out, dl])

        # — 自选股
        with gr.Tab("📋 自选"):
            disp = gr.Textbox(label="列表", value=",".join(load_stocks()), lines=3)
            with gr.Row():
                ain=gr.Textbox(label="添加"); ab=gr.Button("加")
            with gr.Row():
                rin=gr.Textbox(label="移除"); rb=gr.Button("删")
            sm=gr.Textbox(label="状态")
            ab.click(lambda a,b: add_stock(a,b), [ain,disp], [disp,sm])
            rb.click(lambda a,b: rm_stock(a,b), [rin,disp], [disp,sm])

        # — AI 对话
        with gr.Tab("💬 AI对话"):
            gr.Markdown("### 🤖 投顾（已连组合/自选/选股数据）")
            bot = gr.Chatbot(height=500)
            msg = gr.Textbox(label="问题", placeholder="如：我的组合风险大吗？")
            sb = gr.Button("发送", variant="primary")
            cb = gr.Button("清空")
            hist = gr.State([])
            def _send_wrapper(msg, hist): return ai_chat(msg, hist, portfolio_in.value)
            sb.click(lambda m, h: _send_wrapper(m, h), [msg, hist], [msg, bot])
            msg.submit(lambda m, h: _send_wrapper(m, h), [msg, hist], [msg, bot])
            cb.click(lambda: ([],[]), None, [bot, hist])

        # — 多周期（新）
        build_multiperiod_tab()
        # — 每日推荐（新）
        build_recommend_tab()
        # — 推荐跟踪（新）
        build_track_tab()
        # — 参数优化（新）
        build_optimize_tab()
        # — 多因子 IC（新）
        build_factor_tab()
        # — 策略实验室（新）
        build_strategy_lab_tab()
        # — 回测（新）
        build_backtest_tab()

# ====== 12. 启动 ======
if __name__ == "__main__":
    # 收盘后自动跑每日推荐（默认 16:00，交易日；ASTOCK_AUTO_RECOMMEND=0 可关闭）
    start_recommend_scheduler()
    port = 7861
    for p in range(port, port+10):
        try:
            app.launch(server_port=p, share=False, theme="soft")
            break
        except OSError:
            print(f"端口{p}被占，试{p+1}")
    else:
        print("❌ 无可用端口")
