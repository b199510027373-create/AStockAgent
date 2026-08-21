import pandas as pd

def test_ma_logic():
    print("=== 开始测试 MA5 > MA20 逻辑 ===")
    
    # 模拟从你的 stocks.csv 读取的数据
    # 注意：这里我手动构造了一些数据来模拟真实情况
    data = {
        'code': ['600000.SH', '000001.SZ'],
        'close': [10.5, 11.2],
        'MA5': [10.3, 11.5],   # 第一个 False (10.3 < 10.5), 第二个 True (11.5 > 11.2)
        'MA20': [10.4, 11.0],  # 第一个 False (10.4 > 10.5 is False), 第二个 True
        'RSI': [45, 55]
    }
    
    df = pd.DataFrame(data)
    
    # 这是你在 app.py 里可能用到的筛选逻辑
    # 注意：在 Pandas 中，比较浮点数要用 Series，并且要注意方向
    # MA5 > MA20 意味着短期均线上穿长期均线
    
    # 模拟你的筛选逻辑
    if 'MA5' in df.columns and 'MA20' in df.columns:
        # 这里的逻辑要和你的 app.py 保持一致
        # 注意：MA5 > MA20 通常是金叉或强势，但这里要看具体定义
        condition = (df['MA5'] > df['MA20'])
        
        print("\n原始数据：")
        print(df)
        
        print("\n筛选条件：MA5 > MA20")
        print(condition)
        
        filtered_df = df[condition]
        
        print(f"\n筛选结果：找到 {len(filtered_df)} 只股票")
        print(filtered_df)
    else:
        print("❌ 错误：找不到 MA5 或 MA20 列！")

if __name__ == "__main__":
    test_ma_logic()