# -*- coding: utf-8 -*-
"""验证：复用主脚本函数跑回测，把指标与交易明细直接写入文件（规避终端捕获问题）。"""
import btc_contract_backtest as bt

df = bt.fetch_data()
df = bt.generate_signals(df)
df, trades = bt.run_backtest(df)
m = bt.calc_metrics(df, trades)

# 先落盘指标与交易明细（即使后续绘图失败也有结果）
with open("verify_report.txt", "w", encoding="utf-8") as f:
    f.write("== BTC 合约策略回测结果 ==\n")
    for k, v in m.items():
        f.write("{:<10}: {}\n".format(k, v))
    f.write("\n交易笔数: {}\n".format(len(trades)))
    if trades:
        import pandas as pd
        d = pd.DataFrame(trades)[["entry_time", "entry_price", "exit_price",
                                  "side", "reason", "pnl", "return", "bars_held"]]
        d["side"] = d["side"].map({1: "多", -1: "空"})
        f.write("\n交易明细（前 10 笔）:\n")
        f.write(d.head(10).to_string(index=False) + "\n")

# 绘图（失败不影响指标落盘）
try:
    bt.plot_results(df, trades)
    with open("verify_report.txt", "a", encoding="utf-8") as f:
        f.write("\n图表: backtest_result.png\n")
except Exception as e:
    with open("verify_report.txt", "a", encoding="utf-8") as f:
        f.write("\n绘图失败: {}\n".format(e))
print("verify done")
