# 🎬 电影雷达

盯住你常去的影院：还没上映的新片一开票就推送到 iPhone；同时汇总豆瓣、IMDb、烂番茄评分，以及猫眼、淘票票的想看人数；看完的电影可以收进票夹，生成票根、解锁成就。
全部跑在 GitHub 上，免费，不需要自己的服务器或电脑。

## 它会做什么

| 功能 | 频率 |
|---|---|
| 关注影院有**未上映**的新片第一次开票，推送一次（已上映的不管，每天加场也不提醒） | 每 5～10 分钟检查（见下面"让提醒更准时"） |
| **星标**的电影开分了（豆瓣 / IMDb / 烂番茄第一次出分） | 跟随评分刷新 |
| 豆瓣、IMDb、烂番茄评分 | 星标 / 已开票的 2～4 小时刷新，其他 8～12 小时 |
| 猫眼、淘票票想看人数，以及较昨天的变化 | 约每 25 分钟 |
| 影院检查或猫眼列表连续失败 1 小时，推送"检查失败"；恢复后推送"已恢复" | 跟随检查 |

- 星标电影开票时，Bark 会**持续响铃**约 30 秒，方便抢首日场（`config.json` 里可改）。
- 一条开票推送里只有一部片时，点推送直接打开猫眼这家影院的排片页（已选好这部电影），挑场次就能选座；网页详情页"你的影院"里也有"选座"按钮。
- 开票提醒可以在网页"设置"里切换成"只推送星标的电影"。不管推不推送，所有开票和开分都会列在网页的"动态"里，星标的会在你自己的手机上高亮。
- 第一次运行后，评分要跑几个小时才会补全（为了不被豆瓣、猫眼限流，每轮只查几部）。

## 两个仓库，各管各的

| 仓库 | 公开还是私有 | 放什么 |
|---|---|---|
| `movie-radar`（本仓库） | **公开**（GitHub Pages 和 Actions 才免费） | 网页、抓取程序、公开的电影数据 |
| `movie-radar-data` | **私有** | 你的星标、票夹（哪天在哪看、座位、短评）、海报备份 |

公开仓库和网站上不会出现你星标了什么、看过什么。

## 第一次设置（大约 15 分钟）

1. **Bark 推送**：在 App Store 装 **Bark**，复制里面网址中间那串 key（形如 `https://api.day.app/这一段/`）。
2. **私有数据仓库**：在 GitHub 新建一个 **Private** 仓库，名字叫 `movie-radar-data`，空的就行。
3. **建一个密钥**：GitHub → Settings → Developer settings → Fine-grained tokens → Generate new token
   - Expiration 选最长
   - Repository access 选 Only select repositories，把 `movie-radar` 和 `movie-radar-data` 都选上
   - Permissions：**Contents** 选 Read and write，**Actions** 选 Read and write
   - 生成后复制下来（只显示一次）。下面几处都用这一个密钥。
4. **填到本仓库**：Settings → Secrets and variables → Actions
   - Secrets 里新建 `BARK_KEY`，值是 Bark 的 key（家人也想收，用英文逗号隔开多个）
   - Secrets 里新建 `DATA_TOKEN`，值是第 3 步的密钥
   - Variables 里新建 `DATA_REPO`，值是 `你的用户名/movie-radar-data`
5. **打开网页**：Settings → Pages → Build and deployment → Source 选 **GitHub Actions**。
6. **跑第一次**：Actions → 更新电影数据 → Run workflow（运行方式选 `force`）。跑完手机会收到"✅ 电影雷达已启动"。
7. **装到主屏幕并连接**：用 Safari 打开 `https://你的用户名.github.io/movie-radar/`，分享 → 添加到主屏幕。
   从主屏幕打开，点右上角"设置"，粘贴第 3 步的密钥（主屏幕版和 Safari 里的数据不互通，在哪个里面用就在哪个里面填）。

只想试推送：Run workflow 时运行方式选 `test-push`。

## 让提醒更准时（建议配置）

GitHub 自带的定时任务在高峰期会晚 10～20 分钟。用免费的 [cron-job.org](https://cron-job.org) 每 5 分钟叫它跑一次，会准时很多（GitHub 自带的定时继续留着兜底）：

1. 注册 cron-job.org，Create cronjob。
2. URL：`https://api.github.com/repos/你的用户名/movie-radar/actions/workflows/update.yml/dispatches`
3. Schedule：Every 5 minutes。
4. Advanced → Request method 选 **POST**；Headers 加三行：
   - `Authorization: Bearer 第 3 步的密钥`
   - `Accept: application/vnd.github+json`
   - `Content-Type: application/json`
5. Request body：`{"ref":"main"}`
6. 保存后点 Test run，返回 204 就成功了。

抓取频率不会因此变高太多：评分和电影列表各自有最短间隔，每轮主要是查两家影院的排片。

## 长期不用也不会停

GitHub 规定公开仓库 60 天没有活动会停掉定时任务。程序每 25 天会自动往仓库提交一次时间戳来保活，平时不用管。万一被停了，到 Actions 页面点 Enable workflow 就好。

## 星标和票夹

- **星标**：打开任意电影 → 右上角"☆ 星标"。星标的电影开票、开分都会推送。改动后 1～2 分钟生效（密钥有 Actions 权限时会立刻触发一次检查）。
- **票夹**：电影页点"看过了，收进票夹"，记下哪天、在哪家影院、座位、评分和一句话，就会生成一张票根：底色取自海报，带编号、评分、条形码和"已观影"印章，可以存成图片分享。
  - 票根有横版和竖版，可以随时切换；纸纹随电影类型变化；第 10 张镭射纸、第 50 张黑卡烫金、每第 100 张硫酸纸。
  - 编号在收进票夹时就定下来，以后补记旧电影也不会打乱前面的编号。
  - 海报小图会备份到私有仓库，猫眼的图片链接以后失效了，票根也不会变空白。
  - "票夹"页有观影统计和 18 个成就，点任意成就能看到怎么解锁。
- 断网时记的票根、删掉的票根会先存在手机上，联网后自动合并到私有仓库，不会互相覆盖。
- 没连 GitHub 时票根只存在浏览器里，Safari 长期不打开可能被系统清掉，建议尽早连接。

## 改设置：编辑 `config.json`

在 GitHub 网页上点开 `config.json` → 右上角铅笔图标 → 改完点 Commit changes，会自动重新跑一次。

- `cinemas`：关注的影院。`id` 是猫眼的影院编号（猫眼网页版影院链接里那串数字），`short` 是推送和网页上显示的短名。
- `watch`：也可以在这里写片名当作星标，例如 `["奥德赛"]`（一般直接在网页上点星标就行；注意这里是公开的）。
- `notify.new_ticketing`：开票提醒总开关，默认开。
- `notify.starred_ticket_alert`：星标电影开票时的提醒方式。`call` = 持续响铃约 30 秒（默认），`critical` = 持续响铃并且静音模式也响（需要在 Bark 里允许"重要警告"），`normal` = 普通提醒。
- `notify.score_open`：开分提醒的范围。`watch` = 只推星标的（默认），`opened` = 星标的 + 已在你影院开票的，`all` = 全部，`off` = 关闭。
- `notify.new_coming_movies`：西安新定档的片子要不要推送，默认关。
- `imdb_overrides`：IMDb 配错了可以手动指定，例如 `{"奥德赛": "tt33764258"}`。

## 数据从哪来

- 猫眼手机版接口：热映 / 待映列表、想看人数、影院排片（用来判断开票）。
- 淘票票 H5 接口：想看人数。
- 豆瓣：搜索建议 + 移动版接口取评分。
- IMDb：搜索建议接口找片，评分来自 IMDb 官方每日数据集（datasets.imdbws.com，非商业用途免费）。
- 烂番茄：网页上的专业评分（Tomatometer）和观众评分（Popcornmeter）。
- Wikidata：用 IMDb 编号查对应的豆瓣、烂番茄条目，减少配错。

这些都不是官方开放接口，平台改版后可能失效。挂了会推送提醒，网页最下面也会显示每个来源的状态；把报错发给 Claude 修一下就行。

仅供个人使用，请不要公开传播或商用，也不要把抓取频率调得更高。

## 文件说明

```
config.json                   你的设置
scraper/run.py                每次运行的主流程（抓取、比对、推送、输出）
scraper/sources.py            猫眼 / 淘票票 / 豆瓣 / IMDb / 烂番茄的接口
scraper/notify.py             Bark 推送
docs/index.html               手机网页
.github/workflows/update.yml  定时任务
.github/keepalive.txt         保活用的时间戳（自动更新）

私有仓库 movie-radar-data（网页自动维护）：
watch.json                    星标和开票提醒模式
diary.json                    票夹
posters/                      海报备份
```

运行产生的数据存在本仓库的 `radar-data` 分支，每次覆盖，不会让仓库越来越大。
