# AStockAgent

A 股智能投顾本地系统，基于 Gradio + Tushare + DeepSeek。支持单股全指标体检、组合体检、策略选股、AI 对话与每日简报。

> 仅供学习研究，不构成任何投资建议。

## 功能

- 单股体检：基础 K 线图（K线/MA/布林带/成交量）与全指标视图（MACD/KDJ/RSI/CCI/VR/PSY/OBV/ATR），AI 技术面点评
- 组合体检：权重配置、AI 资产配置建议、净值曲线、相关性热力图、年化波动率
- 策略选股：基于自选股池的 11 条件复合过滤（趋势、动量、波动、量能、心理），支持 CSV 导出
- AI 对话：上下文接入自选股与组合数据
- 每日简报：对自选股池生成 AI 简报

## 环境要求

- Windows
- Python 3.10+
- 可用的 Tushare Pro 积分与 DeepSeek API Key

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## 配置

复制 `.env.example` 为 `.env` 并填写自己的密钥，或直接设置环境变量：

```dotenv
TUSHARE_TOKEN=your_tushare_token
DEEPSEEK_API_KEY=your_deepseek_api_key
DEEPSEEK_BASE=https://api.deepseek.com
TUSHARE_PROXY_URL=
```

代码会在启动时读取环境变量；未配置时使用占位值，数据请求将无法正常工作。

## 运行

```powershell
python app.py
```

或双击 `start_app.bat`，启动后访问 http://127.0.0.1:7861 。

## 目录结构

```text
app.py             Gradio 主应用
indicators.py      技术指标工厂
ts_init.py         Tushare 数据引擎（带本地缓存）
stocks.csv         自选股池示例
data/              本地行情缓存示例
notebooks/         数据与指标开发笔记
```

## 免责声明

本项目仅用于技术学习与研究，所有输出均不构成投资建议。股市有风险，投资需谨慎。