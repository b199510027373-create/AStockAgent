import pandas as pd
import numpy as np

class IndicatorFactory:
    """技术指标工厂"""

    @staticmethod
    def calculate_ma(df, windows=[5, 10, 20, 60]):
        for w in windows:
            df[f'ma{w}'] = df['close'].rolling(window=w).mean()
        return df

    @staticmethod
    def calculate_rsi(df, periods=[14]):
        for p in periods:
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0)
            loss = -delta.where(delta < 0, 0)
            avg_gain = gain.rolling(window=p).mean()
            avg_loss = loss.rolling(window=p).mean()
            rs = avg_gain / avg_loss
            df[f'rsi{p}'] = 100 - (100 / (1 + rs))
        return df

    @staticmethod
    def calculate_macd(df):
        ema_fast = df['close'].ewm(span=12, adjust=False).mean()
        ema_slow = df['close'].ewm(span=26, adjust=False).mean()
        df['macd'] = ema_fast - ema_slow
        df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
        df['macd_hist'] = df['macd'] - df['macd_signal']
        return df

    @staticmethod
    def add_all_indicators(df, config=None):
        if config is None:
            config = {'ma': [5, 10, 20], 'rsi': [14], 'macd': True}
        if 'ma' in config:
            df = IndicatorFactory.calculate_ma(df, config['ma'])
        if 'rsi' in config:
            df = IndicatorFactory.calculate_rsi(df, config['rsi'])
        if config.get('macd'):
            df = IndicatorFactory.calculate_macd(df)
        return df

print("✅ 指标工厂加载成功")
