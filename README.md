# 🎬 电影雷达

盯住你常去的影院：还没上映的新片一开票就推送到 iPhone；同时汇总豆瓣、IMDb、烂番茄评分，以及猫眼、淘票票的想看人数。
全部跑在 GitHub 上，免费，不需要自己的服务器或电脑。

## 它会做什么

| 功能 | 频率 |
|---|---|
| 关注影院有**未上映**的新片第一次开票，推送一次（已上映的片子不管，每天加场也不提醒） | 每 10 分钟左右检查 |
| 豆瓣、IMDb、烂番茄评分 | 已开票 / 关注的片子 2～4 小时刷新，其他 8～12 小时 |
| 猫眼、淘票票想看人数，以及较昨天的变化 | 约每 25 分钟 |
| 已开票或关注的片子开分了（豆瓣 / IMDb / 烂番茄） | 跟随评分刷新 |

第一次运行后，评分要跑几个小时才会补全（为了不被豆瓣、猫眼限流，每轮只查几部）。
GitHub 的定时任务在高峰期可能晚 10～20 分钟才跑，抢首日首场的话别完全依赖它。

## 第一次设置（大约 10 分钟）

1. **Bark 推送**：在 App Store 装 **Bark**，打开后复制里面网址中间那串 key（形如 `https://api.day.app/这一段/`）。
2. **填 key**：仓库页面 → Settings → Secrets and variables → Actions → New repository secret
   名字填 `BARK_KEY`，值填刚才的 key。家人也想收，就用英文逗号隔开多个 key。
3. **打开网页**：Settings → Pages → Build and deployment → Source 选 **GitHub Actions**。
4. **跑第一次**：Actions → 更新电影数据 → Run workflow（运行方式选 `force`）。
   跑完手机会收到"✅ 电影雷达已启动"。之后会每 10 分钟自动运行。
5. **装到主屏幕**：用 Safari 打开 `https://你的用户名.github.io/仓库名/`，点分享 → 添加到主屏幕。

只想试推送：Run workflow 时运行方式选 `test-push`。

## 改设置：编辑 `config.json`

在 GitHub 网页上点开 `config.json` → 右上角铅笔图标 → 改完点 Commit changes，会自动重新跑一次。

- `cinemas`：关注的影院。`id` 是猫眼的影院编号（猫眼网页版影院链接里那串数字），`short` 是推送和网页上显示的短名。
- `watch`：重点关注的片名，例如 `["奥德赛", "疯狂动物城2"]`。这些片子开分会推送，开票推送前面会加 ⭐。
- `notify.new_ticketing`：开票提醒，默认开。
- `notify.score_open`：开分提醒的范围。`opened` = 已在你影院开票的片子 + watch 里的（默认），`watch` = 只看 watch 里的，`all` = 全部，`off` = 关闭。
- `notify.new_coming_movies`：西安新定档的片子要不要推送，默认关。
- `imdb_overrides`：IMDb 配错了可以手动指定，例如 `{"奥德赛": "tt33764258"}`。

## 数据从哪来

- 猫眼手机版接口：热映 / 待映列表、想看人数、影院排片（用来判断开票）。
- 淘票票 H5 接口：想看人数。
- 豆瓣：搜索建议 + 移动版接口取评分。
- IMDb：搜索建议接口找片，评分来自 IMDb 官方每日数据集（datasets.imdbws.com，非商业用途免费）。
- 烂番茄：网页上的专业评分（Tomatometer）和观众评分（Popcornmeter）。
- Wikidata：用 IMDb 编号查对应的豆瓣、烂番茄条目，减少配错。

这些都不是官方开放接口，平台改版后可能失效。网页最下面会显示每个来源最近一次是否成功；哪个挂了，把报错发给 Claude 修一下就行。

仅供个人使用，请不要公开传播或商用，也不要把抓取频率调得更高。

## 文件说明

```
config.json                 你的设置
scraper/run.py              每次运行的主流程（抓取、比对、推送、输出）
scraper/sources.py          猫眼 / 淘票票 / IMDb 的接口
scraper/notify.py           Bark 推送
docs/index.html             手机网页
.github/workflows/update.yml  定时任务
```

运行产生的数据存在 `radar-data` 分支，每次覆盖，不会让仓库越来越大。
