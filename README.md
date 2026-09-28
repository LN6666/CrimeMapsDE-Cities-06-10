# CrimeMapsDE-Cities-06-10

德国城市犯罪地图工程的第 2 组独立代码仓库，城市顺序为 **Düsseldorf、Stuttgart、Leipzig、Dortmund、Bremen**。仓库处理五城的警方公告来源和本地检查点；**没有五城地图，也没有发布数据**。第 1 组的柏林入口及城市切换由其仓库负责。

| 城市 | 当前进度 | 来源 |
| --- | --- | --- |
| Düsseldorf | 警方署名新闻室采集；逐篇市域证据审核门禁 | [Polizei Düsseldorf 新闻室](https://www.presseportal.de/blaulicht/nr/13248) |
| Stuttgart | 警方署名新闻室采集；列表地域仅作审核线索 | [Polizeipräsidium Stuttgart 新闻室](https://www.presseportal.de/blaulicht/nr/110977) |
| Leipzig | 官方材料本地暂存；网站 robots.txt 当前返回 404，自动采集关闭 | [萨克森警方档案](https://www.polizei.sachsen.de/de/111350.htm) → [官方 Medienservice](https://medienservice.sachsen.de/medien/?search%5Binstitution_ids%5D%5B%5D=10976) |
| Dortmund | 官方原生档案有界采集、断点与市域审核门禁 | [Polizei Dortmund 原生档案](https://dortmund.polizei.nrw/presse/pressemitteilungen) |
| Bremen | 官方分组档案页本地暂存；网站 robots.txt 当前为空，自动采集关闭 | [Polizei Bremen 原生档案](https://www.polizei.bremen.de/news/pressestelle/pressearchiv-5034) |

警方新闻室和警察分局档案可以含市外、高速公路和多事件公告；发布机构或列表地域不证明案发地点在目标城市。尤其 Dortmund 的原生档案含市外调查，Leipzig 的同一媒体通报可同时列出市内及周边县案件，Bremen 的一个档案页面可合并多篇公告。采集数据仅在本地暂存，必须逐篇核对原文、定位与市域范围，再经项目所有者检查和确认，才能考虑生成新地图。公告也不是完整报案清单或一篇对应一宗犯罪。

## 本地运行

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。

```sh
uv sync --locked
export PYTHONPATH="$PWD/src"
uv run pytest -q
uv run ruff check src tests

# 有界试跑；每次最多处理指定页数和篇数，未抓完时退出码 2 属预期门禁。
uv run python -m crimemapsde_cities_06_10.dusseldorf --year 2026 --full --max-pages 2 --limit 5
uv run python -m crimemapsde_cities_06_10.stuttgart --year 2026 --pages 2 --limit 5
uv run python -m crimemapsde_cities_06_10.dortmund --year 2026 --pages 1 --limit 1
```

默认检查点位于 `.runtime/cities/<城市模块名>/police.sqlite`。断点重跑保留来源文章 ID、URL、发布时间、原文、修订哈希及失败状态。自动采集前检查 `robots.txt`，按最少一秒间隔请求；来源不可用或解析失败时保留已有记录。Dortmund 记录列表 URL 标识和文章原生 Drupal 节点 ID；列表里的警方编号只是溯源字段。

Düsseldorf 和 Dortmund 的市域结论必须引用当前原文，修订后旧结论失效；Stuttgart 的列表地域只存为待审核线索。此仓库没有地理编码、逐篇内容复核与所有者发布批准的完整流程，因而任何本地记录都不构成可发布地图。

## Leipzig 和 Bremen 的离线入口

两城的官方档案仍可供人在浏览器中查阅，但本程序当前无法验证其 robots 规则，因此**不会自动请求档案或文章**。可以将已人工核对的官方材料存为 Git 忽略目录中的 JSONL，再限量导入本地库：

```sh
uv run python -m crimemapsde_cities_06_10.leipzig --input .runtime/cities/leipzig/import.jsonl --limit 10
uv run python -m crimemapsde_cities_06_10.bremen --input .runtime/cities/bremen/import.jsonl --limit 10
```

每行 JSON 对象需有 `publisher`、`url`、带时区的 `published`、`title`、`body`。Leipzig 的 `publisher` 固定为 `Polizeidirektion Leipzig`，URL 形如 `https://medienservice.sachsen.de/medien/news/1100200`；Bremen 的 `publisher` 固定为 `Polizei Bremen`，URL 是官方分组页面，例如 `https://www.polizei.bremen.de/news/pressestelle/pressemeldungen-ab-11092026-69054`。导入器按输入文件哈希和行号断点续跑，重复导入会检测修订；它无法自行验证人工提供的原文与官方网页相同。Leipzig 通报和 Bremen 分组页一律标为多事件来源单元，保持不可发布，也不会从一个页面推断出一个案发点。两城历史覆盖仍不完整。网站 robots 恢复前不启用在线爬取。

原文数据库、运行档案、下载材料、账户凭据和生成城市数据均由 `.gitignore` 排除，不进入 Git。CI 只用合成输入运行测试和静态检查，不抓取网站。

代码采用 Apache-2.0 许可证，见 [LICENSE](LICENSE)。
