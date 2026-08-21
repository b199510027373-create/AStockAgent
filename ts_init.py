# ts_init.py
import tushare as ts
import pandas as pd
import os
from datetime import datetime, timedelta

TOKEN = os.getenv("TUSHARE_TOKEN", "your_tushare_token")
PROXY_URL = os.getenv("TUSHARE_PROXY_URL", "")

# 数据存放目录
DATA_DIR = "data"
CACHE_DAYS = 1  # 缓存有效期（天），设为1表示一天内不重复拉取

def get_pro():
    """获取 Tushare Pro 客户端"""
    pro = ts.pro_api(TOKEN)
    if PROXY_URL:
        pro._DataApi__http_url = PROXY_URL
    return pro

def ensure_data_dir():
    """确保数据目录存在"""
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)

def get_cache_path(ts_code):
    """生成缓存文件路径"""
    return os.path.join(DATA_DIR, f"{ts_code.replace('.', '_')}.csv")

def is_cache_valid(ts_code):
    """检查缓存是否有效（是否在有效期内）"""
    cache_path = get_cache_path(ts_code)
    if not os.path.exists(cache_path):
        return False
    
    # 检查文件修改时间
    file_time = datetime.fromtimestamp(os.path.getmtime(cache_path))
    now = datetime.now()
    
    # 如果是当天的数据，且时间在 15:30 之后，认为有效
    if file_time.date() == now.date() and now.hour >= 15:
        return True
    # 如果是前一天的数据，且当前时间在 9:00 之前，认为有效
    if (now - file_time).days < CACHE_DAYS and now.hour < 9:
        return True
    
    return False

def fetch_stock_data(ts_code, start_date="20250101", end_date="20260601", force_refresh=False):
    """
    通用股票数据拉取函数（带缓存）
    
    Args:
        ts_code: 股票代码（如 600519.SH）
        start_date: 开始日期
        end_date: 结束日期
        force_refresh: 是否强制刷新缓存
    """
    ensure_data_dir()
    cache_path = get_cache_path(ts_code)
    
    # 如果缓存有效且不强制刷新，直接读取缓存
    if not force_refresh and is_cache_valid(ts_code):
        try:
            df = pd.read_csv(cache_path)
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            print(f"✅ 从缓存加载 {ts_code}，共 {len(df)} 行")
            return df
        except Exception as e:
            print(f"⚠️ 缓存读取失败，重新拉取: {e}")
    
    # 缓存无效或强制刷新，重新拉取
    print(f"🔄 正在从 Tushare 拉取 {ts_code}...")
    pro = get_pro()
    
    try:
        df = pro.daily(
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date
        )
        
        if df.empty:
            print(f"❌ {ts_code} 无数据返回")
            return pd.DataFrame()
        
        # 保存缓存
        df.to_csv(cache_path, index=False)
        print(f"✅ {ts_code} 数据已保存，共 {len(df)} 行")
        
        return df
        
    except Exception as e:
        print(f"❌ {ts_code} 拉取失败: {e}")
        return pd.DataFrame()

def batch_fetch_stocks(ts_codes, start_date="20250101", end_date="20260601"):
    """批量拉取多只股票数据"""
    results = {}
    for ts_code in ts_codes:
        df = fetch_stock_data(ts_code, start_date, end_date)
        if not df.empty:
            results[ts_code] = df
    return results

print("✅ AStockAgent 数据引擎已就绪")