# 🎬 电影雷达

盯住你常去的影院：还没上映的新片一开票就推送到 iPhone；同时汇总豆瓣、IMDb、烂番茄评分，以及猫眼、淘票票的想看人数。
全部跑在 GitHub 上，免费，不需要自己的服务器或电脑。

## 它会做什么

| 功能 | 频率 |
|---|---|
| 关注影院有**未上映**的新片第一次开票，推送一次（已上映的不管，每天加场也不提醒） | 每 10 分钟左右检查 |
| **星标**的电影开分了（豆瓣 / IMDb / 烂番茄第一次出分） | 跟随评分刷新 |
| 豆瓣、IMDb、烂番茄评分 | 星标 / 已开票的 2～4 小时刷新，其他 8～12 小时 |
| 猫眼、淘票票想看人数，以及较昨天的变化 | 约每 25 分钟 |

开票提醒可以在网页"设置"里切换成"只推送星标的电影"。
不管推不推送，所有开票和开分都会列在网页的"动态"里，星标的会高亮。

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

## 星标电影（网页里点）

打开任意电影 → 右上角"☆ 星标"。星标的电影开票、开分都会推送给你。

星标要存到仓库里后台才看得到，所以第一次需要在网页"设置"里连接 GitHub（设置页里有一步步的说明）：
GitHub → Settings → Developer settings → Fine-grained tokens → Generate new token，
Repository access 只选这个仓库，Permissions 里 **Contents** 选 **Read and write**，生成后粘贴到网页设置里。
密钥只存在你手机的浏览器里；星标存在仓库根目录的 `watch.json`，改动后 1～2 分钟生效。

## 票夹：看过的电影和票根

在电影页点"看过了，收进票夹"，记下哪天、在哪家影院、座位、评分和一句话，就会生成一张票根：
底色取自海报，带编号、评分、条形码和"已观影"印章，可以存成图片分享。
"票夹"页有观影统计和 18 个成就（首映日、二刷、连场、双城记、高分猎手……）。

票夹存在仓库根目录的 `diary.json`（连接 GitHub 后自动同步；没连接时先存在手机上）。改动它不会触发抓取。

## 改设置：编辑 `config.json`

在 GitHub 网页上点开 `config.json` → 右上角铅笔图标 → 改完点 Commit changes，会自动重新跑一次。

- `cinemas`：关注的影院。`id` 是猫眼的影院编号（猫眼网页版影院链接里那串数字），`short` 是推送和网页上显示的短名。
- `watch`：也可以在这里写片名当作星标，例如 `["奥德赛"]`（一般直接在网页上点星标就行）。
- `notify.new_ticketing`：开票提醒总开关，默认开。
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

这些都不是官方开放接口，平台改版后可能失效。网页最下面会显示每个来源最近一次是否成功；哪个挂了，把报错发给 Claude 修一下就行。

仅供个人使用，请不要公开传播或商用，也不要把抓取频率调得更高。

## 文件说明

```
config.json                 你的设置
watch.json                  星标列表和开票提醒模式（网页自动维护）
diary.json                  看过的电影 / 票根（网页自动维护）
scraper/run.py              每次运行的主流程（抓取、比对、推送、输出）
scraper/sources.py          猫眼 / 淘票票 / IMDb 的接口
scraper/notify.py           Bark 推送
docs/index.html             手机网页
.github/workflows/update.yml  定时任务
```

运行产生的数据存在 `radar-data` 分支，每次覆盖，不会让仓库越来越大。
