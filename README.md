# 🎬 电影雷达

盯住你常去的影院：一开票就推送到 iPhone，同时汇总猫眼、淘票票、IMDb 的评分和想看人数。
全部跑在 GitHub 上，免费，不需要自己的服务器或电脑。

## 它会做什么

| 功能 | 频率 |
|---|---|
| 检查关注影院的排片，有新片开票就推送 | 每 10 分钟左右 |
| 猫眼 / 淘票票评分、想看人数、较昨天的变化 | 约每 25 分钟 |
| IMDb 评分（官方数据集，每天更新） | 约每 3 小时 |
| 关注的片子开分、加场时推送 | 跟随上面的频率 |

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
- `watch`：重点关注的片名，例如 `["奥德赛", "疯狂动物城2"]`。这些片子开分、加场都会推送，在列表里置顶。
- `notify.score_open`：开分提醒的范围。`my_cinemas` = 在你影院排片的片子（默认），`watch` = 只看 watch 里的，`all` = 全部，`off` = 关闭。
- `notify.new_coming_movies`：西安新定档的片子要不要推送，默认关。
- `imdb_overrides`：IMDb 配错了可以手动指定，例如 `{"奥德赛": "tt33764258"}`。

## 数据从哪来

- 猫眼手机版接口：热映 / 待映列表、评分、想看、影院排片。
- 淘票票 H5 接口：评分、想看。
- IMDb：搜索建议接口找片，评分来自 IMDb 官方每日数据集（datasets.imdbws.com，非商业用途免费）。

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
