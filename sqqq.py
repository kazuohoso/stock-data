import yfinance as yf
import pandas as pd

df = yf.Ticker('SQQQ').history(period='max', interval='1d')
df.index = df.index.tz_localize(None)
df = df[['Open','High','Low','Close','Volume']]
df.index.name = 'Date'
df.to_csv('SQQQ_daily.csv')
print('完了！', len(df), '日分のデータを保存しました')
print(df.tail(3))
input('Enterキーで閉じる')