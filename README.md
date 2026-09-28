# CrimeMapsDE-Cities-06-10

德国城市犯罪地图工程的第 2 组独立代码仓库，城市顺序为 **Düsseldorf、Stuttgart、Leipzig、Dortmund、Bremen**。仓库处理五城的警方公告来源和本地检查点；**没有五城地图，也没有发布数据**。第 1 组的柏林入口及城市切换由其仓库负责。

| 城市 | 当前进度 | 来源 |
| --- | --- | --- |
| Düsseldorf | 警方署名新闻室采集；逐篇市域证据审核门禁 | [Polizei Düsseldorf 新闻室](https://www.presseportal.de/blaulicht/nr/13248) |
| Stuttgart | 警方署名新闻室采集；列表地域仅作审核线索 | [Polizeipräsidium Stuttgart 新闻室](https://www.presseportal.de/blaulicht/nr/110977) |
| Leipzig | 官方 Medienservice 的公开日期检索有界采集；逐篇场景与市域审核门禁 | [萨克森警方档案](https://www.polizei.sachsen.de/de/111350.htm) → [官方 Medienservice](https://www.medienservice.sachsen.de/medien/?search%5Binstitution_ids%5D%5B%5D=10976) |
| Dortmund | 官方原生档案有界采集、断点与市域审核门禁 | [Polizei Dortmund 原生档案](https://dortmund.polizei.nrw/presse/pressemitteilungen) |
| Bremen | 警方署名 Presseportal 新闻室可逐篇断点采集；原生分组档案因空 robots.txt 只作人工补充 | [Polizei Bremen 新闻室](https://www.presseportal.de/blaulicht/nr/35235) / [原生档案](https://www.polizei.bremen.de/news/pressestelle/pressearchiv-5034) |

警方新闻室和警察分局档案可以含市外、高速公路和多事件公告；发布机构或列表地域不证明案发地点在目标城市。尤其 Dortmund 的原生档案含市外调查，Leipzig 的同一媒体通报可同时列出市内及周边县案件，Bremen 的一个档案页面可合并多篇公告。采集数据仅在本地暂存，必须逐篇核对原文、定位与市域范围，再经项目所有者检查和确认，才能考虑生成新地图。公告也不是完整报案清单或一篇对应一宗犯罪。

## 当前本地来源状态

2026-09-29 的 Git 忽略检查点如下。数量表示已发现公告／已保存正文，不表示市域案件数或地图完成度：

| 城市 | 当前检查点 | 仍待处理 |
| --- | ---: | --- |
| Düsseldorf | 64 / 4 | 60 篇正文、年度遍历、市域与逐篇语义复核 |
| Stuttgart | 60 / 6 | 54 篇正文、年度遍历、市域与逐篇语义复核 |
| Leipzig | 6 / 2 | 仅完成 1 页、2 篇正文的小型实网探针；4 篇待正文，年度遍历与逐篇审核未完成 |
| Dortmund | 20 / 4 | 16 篇正文、年度遍历、市域与逐篇语义复核 |
| Bremen | 639 / 639 | 警方署名新闻室的 2026 遍历已完成；仍需逐篇市域、多场景复核，且不证明原生档案等价 |

这些计数只存在于本机检查点，没有原文、数据库或生成数据进入 Git。所有市域标签均为待 LLM 对照原文的线索，不是批准结论。

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
uv run python -m crimemapsde_cities_06_10.leipzig --live --year 2026 --max-pages 1 --limit 2 --delay 4
uv run python -m crimemapsde_cities_06_10.dortmund --year 2026 --pages 1 --limit 1
uv run python -m crimemapsde_cities_06_10.bremen --year 2026 --full --max-pages 2 --limit 5
```

默认检查点位于 `.runtime/cities/<城市模块名>/police.sqlite`。断点重跑保留来源文章 ID、URL、发布时间、原文、修订哈希及失败状态。自动采集前检查 `robots.txt`；Leipzig 每次重新检查并至少间隔 4 秒，其余现有采集器至少间隔 1 秒。来源不可用或解析失败时保留已有记录。Bremen 自动路径只请求警方署名的 Presseportal 单篇公告，不请求 robots 规则为空的原生分组档案。Dortmund 记录列表 URL 标识和文章原生 Drupal 节点 ID；列表里的警方编号只是溯源字段。

Düsseldorf 和 Dortmund 的市域结论必须引用当前原文，修订后旧结论失效；Stuttgart 的列表地域只存为待审核线索。此仓库没有地理编码、逐篇内容复核与所有者发布批准的完整流程，因而任何本地记录都不构成可发布地图。

## 五城来源契约与只读审计

`registry.py` 固定五城的 slug、显示名、供未来米制 GIS 使用的 EPSG、来源类型和本地库路径。西部四城采用 ETRS89 / UTM 32N（[EPSG:25832](https://epsg.org/crs_25832/ETRS89-UTM-zone-32N.html)）；Leipzig 采用 UTM 33N（[EPSG:25833](https://epsg.org/crs_25833/ETRS89-UTM-zone-33N.html)）。这些 CRS 仅是接口元数据，当前没有计算坐标。

第一组可用只读命令从本地检查点生成版本化 JSON 来源清单：

```sh
uv run python -m crimemapsde_cities_06_10.source_audit --city dortmund \
  --out .runtime/cities/dortmund/source-audit.json
```

省略 `--out` 时写到标准输出；可用 `--db` 指定本地数据库。`schema_version: 1` 的清单包含 `city`、`readiness`、`checkpoint_scans`、`counts`、`records` 和 `location_candidates`。每条记录保留来源 ID、URL、日期、原文 SHA-256、修订号、市域结论及审核状态。Dortmund 另保留原生节点 ID 和警方编号。导出过程只读取数据库，不写入或修改检查点；JSON **不包含原文或市域证据引文**。导出文件应放在 `.runtime/` 等 Git 忽略目录，不能提交或上传。

`location_candidates` 只是供后续逐篇定位审核的来源引用，没有坐标。必须有匹配城市官方来源格式的 URL/ID、带时区的日期、当前原文与匹配哈希、有效修订号和与该哈希绑定的市域证据，才可能出现于该列表。Stuttgart 的列表地域不算市域证据；Leipzig 通报和人工补入的 Bremen 原生分组页是多事件单元，即使后来有市域标注，也不产生定位候选。Bremen 新闻室里的单篇公告仍需当前正文绑定的市域审核。清单内文章复核状态当前一律为 `pending`，所有者仍需检查并批准。组级 `archive_complete` 与 `publication_ready` 仍明确为 `false`；在线逐篇来源核验不代表年度覆盖、语义复核或发布完成。

## Leipzig 公开档案与离线原件入口

Medienservice Sachsen 的 `robots.txt` 当前明确返回 HTTP 404。RFC 9309 §2.3.1.3 将 4xx 视为规则文件不可用并允许抓取器访问；这不同于必须按完全禁止处理的网络错误和 5xx。Leipzig 采集器每次运行都重新检查：只在明确 404/410，或有效的 200 规则允许档案和文章路径时继续；401/403/429、其他 4xx、5xx、网络错误、HTML/空白/损坏规则和 `Disallow` 都会停止。采集器使用网站公开的日期筛选 JSON 后端，固定机构 `10976` 和发布者 `Polizeidirektion Leipzig`，每批限制页数与正文数，顺序请求间隔至少 4 秒，首个来源错误即停止并写入检查点。

该公开档案无须注册或登录；注册只与订阅推送等功能有关。网页动态 token 只记录为本次原始 HTML 哈希，不触发正文修订；稳定修订哈希由规范化 ID、规范 URL、发布者、标题、时间和完整正文生成。采集不会用关键词判断案件、拆案或批准市域，整篇多事件通报进入 LLM 原文审核。

Bremen 的原生档案 robots.txt 为空，仍不自动请求；自动采集使用警方署名的 Presseportal 单篇新闻室。Leipzig 和 Bremen 已人工取得的官方材料也可存为 Git 忽略目录中的 JSONL，再限量导入本地库：

```sh
uv run python -m crimemapsde_cities_06_10.leipzig --input .runtime/cities/leipzig/import.jsonl --limit 10
uv run python -m crimemapsde_cities_06_10.bremen --input .runtime/cities/bremen/import.jsonl --limit 10
```

每行 JSON 对象需有 `publisher`、`url`、带时区的 `published`、`title`、`body`。Leipzig 的 `publisher` 固定为 `Polizeidirektion Leipzig`，URL 形如 `https://medienservice.sachsen.de/medien/news/1100200`；Bremen 的 `publisher` 固定为 `Polizei Bremen`，URL 是官方分组页面，例如 `https://www.polizei.bremen.de/news/pressestelle/pressemeldungen-ab-11092026-69054`。导入器按输入文件哈希和行号断点续跑，重复导入会检测修订；它无法自行验证人工提供的原文与官方网页相同。Leipzig 通报和 Bremen 原生分组页一律标为多事件来源单元，保持不可发布，也不会从一个页面推断出一个案发点。两城历史覆盖仍不完整；任何扫描完成标记都只适用于对应来源渠道，不能证明警方公告全集完整。

Leipzig 的 JSONL 行可再写 `source_file`（相对于 JSONL 的本地 HTML、EML、PDF 或完整 RSS/Atom XML）与该文件的 `source_file_sha256`。入口只读本机字节，不发起网络请求。HTML、邮件和 RSS 的所填正文必须能在文件文字中找到；RSS 若只给摘要，会被拒绝作为完整通报。PDF 只校验签名与字节哈希，`body` 转写必须在逐篇复核时与原 PDF 核对。文件改变会使断点失效并记录新修订，即使转写文字未变。账号、邮箱原件、JSONL 和本地审核输入都只放在 Git 忽略目录。

```sh
uv run python -m crimemapsde_cities_06_10.leipzig \
  --db .runtime/cities/leipzig/police.sqlite \
  --export-review .runtime/cities/leipzig/review-input.ndjson --limit 100
```

只读导出重算正文、修订和原文件哈希，输出整篇通报及来源元数据供 Codex 先读原文、再拆独立案件和所有场景；它不自动给出可定位候选。后续批次可用 `--review-offset` 分页并写不同的本地输出文件。直接在线取得且通过发布者、机构、规范 URL 与哈希门禁的记录标为 `source_verified=true`；人工 JSONL 仍为 `false`。`publication_ready` 始终为 `false`。

原文数据库、运行档案、下载材料、账户凭据和生成城市数据均由 `.gitignore` 排除，不进入 Git。CI 只用合成输入运行测试和静态检查，不抓取网站。

代码采用 Apache-2.0 许可证，见 [LICENSE](LICENSE)。
