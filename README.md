# 科技日报 · 实时版

多源 RSS 聚合的科技资讯静态页，部署在 GitHub Pages，由 GitHub Actions 每 10 分钟自动抓取更新。

## 结构

```
tech-daily/
├── scripts/build.py              # 抓取 RSS → 去重分类 → 生成静态页
├── .github/workflows/update.yml  # 每 10 分钟定时构建并发布
└── public/                       # 构建产物（不提交到 main）
    ├── index.html
    └── version.txt               # 供页面检测新版本
```

## 本地运行

```bash
pip install feedparser
python3 scripts/build.py
open public/index.html
```

## 数据源

量子位、IT之家、少数派、极客公园、钛媒体、InfoQ、开源中国、Solidot、爱范儿、
TechCrunch、The Verge、Ars Technica、Wired、Hacker News。

单个源失败会自动跳过，不影响整体；抓取条数少于 5 条时不覆盖上一版页面。

## 部署

- `main` 分支：源码
- `gh-pages` 分支：构建产物，GitHub Pages 从这里发布
- Actions 每次强制推送单条 commit 到 `gh-pages`，保持历史干净

## 注意

- GitHub 定时任务最小间隔 5 分钟，`*/10` 在高峰期可能延迟几分钟。
- 仓库连续 60 天无活动时 GitHub 会自动停用定时工作流，本项目每次更新都会推送，正常不会触发。
- 国内网络访问 `*.github.io` 可能不稳定，如打不开可再加一份 Cloudflare Pages 部署。
