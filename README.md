# 标普500：起始估值（CAPE）与之后 10 年回报

每周一由 GitHub Actions 自动运行 `build.py`：下载 Shiller 历史数据和 FRED 最新利率/CPI，重算拟合线、差值和跑输概率，生成 `site/index.html` 并发布到 GitHub Pages。

## 文件

| 文件 | 作用 |
| --- | --- |
| `build.py` | 下载数据、计算、生成网页 |
| `template.html` | 网页模板（图表用 d3 绘制） |
| `config.json` | 可调参数：拟合起始年份、手动指定 CAPE 等 |
| `requirements.txt` | Python 依赖 |
| `.github/workflows/update.yml` | 定时任务（每周一 07:43 UTC）+ 发布到 Pages |
| `data/latest.json` | 每次自动运行后提交的数据快照（自动生成） |

## 需要的 Secret

- `FRED_API_KEY`：在 https://fredaccount.stlouisfed.org/apikeys 免费申请。没有它也能运行，但会缺联邦基金利率和 CPI，10 年期国债收益率也会退回到 Shiller 数据里较旧的月度值。

## 本地运行

```bash
pip install -r requirements.txt
export FRED_API_KEY=你的key      # Windows PowerShell: $env:FRED_API_KEY="你的key"
python build.py
# 打开 site/index.html
```

## config.json

- `fit_start`：拟合线起始月份，默认 `1957-01`
- `history_start`：差值走势图起始月份，默认 `1957-01`
- `cape_override` / `cape_override_as_of`：手动指定当前 CAPE（例如 multpl.com 抓取失败时），平时保持 `null`

本页不构成投资建议。
