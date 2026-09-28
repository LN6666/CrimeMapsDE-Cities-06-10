# CrimeMapsDE-Cities-06-10

德国城市犯罪地图工程的第 2 组独立代码仓库，城市顺序为 **Düsseldorf、Stuttgart、Leipzig、Dortmund、Bremen**。当前仓库只有前两城的警方公告来源采集器和本地检查点；**没有五城地图，也没有发布数据**。第 1 组的柏林入口及城市切换由其仓库负责。

| 城市 | 当前进度 | 来源 |
| --- | --- | --- |
| Düsseldorf | 警方署名新闻室采集；逐篇市域证据审核门禁 | [Polizei Düsseldorf 新闻室](https://www.presseportal.de/blaulicht/nr/13248) |
| Stuttgart | 警方署名新闻室采集；列表地域仅作审核线索 | [Polizeipräsidium Stuttgart 新闻室](https://www.presseportal.de/blaulicht/nr/110977) |
| Leipzig | 尚未启动采集器 | — |
| Dortmund | 尚未启动采集器 | — |
| Bremen | 尚未启动采集器 | — |

警方新闻室可以含市外、高速公路和多事件公告；发布机构或列表地域不证明案发地点在目标城市。采集数据仅在本地暂存，必须逐篇核对原文、定位与市域范围，再经项目所有者检查和确认，才能考虑生成新地图。公告也不是完整报案清单或一篇对应一宗犯罪。

## 本地运行

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。

```sh
uv sync --locked
uv run pytest -q
uv run ruff check src tests

# 有界试跑；每次最多处理指定页数和篇数，未抓完时退出码 2 属预期门禁。
uv run python -m crimemapsde_cities_06_10.dusseldorf --year 2026 --full --max-pages 2 --limit 5
uv run python -m crimemapsde_cities_06_10.stuttgart --year 2026 --pages 2 --limit 5
```

默认检查点分别位于 `.runtime/cities/dusseldorf/police.sqlite` 和 `.runtime/cities/stuttgart/police.sqlite`。断点重跑保留来源文章 ID、URL、发布时间、原文、修订哈希及失败状态。采集前检查 `robots.txt`，按最少一秒间隔请求；来源不可用或解析失败时保留已有记录。

Düsseldorf 采集器的 `city_only_reports` 只读取已逐篇审核为市内、且结论哈希仍与当前原文一致的条目。Stuttgart 的列表地域只存为待审核线索。此仓库没有地理编码、逐篇内容复核与所有者发布批准的完整流程，因而任何本地记录都不构成可发布地图。

原文数据库、运行档案、下载材料、账户凭据和生成城市数据均由 `.gitignore` 排除，不进入 Git。CI 只用合成输入运行测试和静态检查，不抓取网站。

代码采用 Apache-2.0 许可证，见 [LICENSE](LICENSE)。
